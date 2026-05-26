"""Minimal Pexip Infinity Client API helper.

Used by the policy server to attach the two side-effects that **must** be
delivered through the Client API:

1. ``set_classification_level`` — applies the meeting-wide classification
   level computed from the caller's domain.
2. ``set_clock`` with ``type: "elapsed"`` — adds an elapsed-time conference
   timer that is visible to every participant.

The helper opens a token (the "Policy Server" participant documented in the
``pexip-policy-server`` skill, SS4), issues the two POSTs, and then keeps the
token alive with periodic ``refresh_token`` calls so the bot stays in the
meeting for its full duration. The loop exits — releasing the token — when
the Pexip node reports the conference has ended (refresh failure).

Host PINs are intentionally **not** sent. PINs are dynamic per meeting and
cannot be configured server-wide. Instead, the policy server's
``participant_properties`` endpoint detects the bot (by display name) and
returns ``role: "chair"`` + ``bypass_lock: true``, so Pexip elevates the
bot to **host** as soon as it joins — regardless of any PIN configuration
on the meeting. This is required because applying classification levels
via the Client API needs host privileges.

Errors are logged but never raised back to the policy request — Pexip must
always receive its policy response on time.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Default token lifetime advertised by Pexip is 120s. Refresh well before
# expiry to tolerate transient network latency.
_DEFAULT_TOKEN_EXPIRES = 120
_REFRESH_SAFETY_MARGIN = 30


class PexipClientAPI:
    """Thin wrapper over the Pexip Client REST API.

    Parameters
    ----------
    node:
        Hostname or IP of a Pexip Conferencing Node (e.g. ``"conf.example.com"``).
    display_name:
        Display name used when joining as the Policy Server participant.
    verify_tls:
        Whether to verify the node's TLS certificate. Defaults to ``True``.
    timeout:
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        node: str,
        display_name: str = "Policy Server",
        verify_tls: bool = True,
        timeout: int = 10,
    ) -> None:
        self.node = node
        self.display_name = display_name
        self.verify_tls = verify_tls
        self.timeout = timeout
        # Per-conference live token held by the keep-alive loop. Lets a
        # later policy callback (e.g. a second participant whose domain
        # lowers the meeting's classification) re-issue
        # ``set_classification_level`` without spawning a second bot.
        # Keyed by conference alias; value is the current token string or
        # ``None`` once the keep-alive loop has released it.
        self._tokens: dict[str, Optional[str]] = {}
        self._tokens_lock = threading.Lock()

    # ------------------------------------------------------------------ utils
    def _base(self, conference_alias: str) -> str:
        return (
            f"https://{self.node}/api/client/v2/conferences/"
            f"{conference_alias}"
        )

    def _post(self, url: str, token: Optional[str], json_body: Optional[dict]) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["token"] = token
        resp = requests.post(
            url,
            headers=headers,
            json=json_body if json_body is not None else {},
            verify=self.verify_tls,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ----------------------------------------------------------------- tokens
    def request_token(self, conference_alias: str) -> tuple[str, int]:
        """Request a token. Returns ``(token, expires_seconds)``.

        No PIN header is sent. PINs are dynamic per meeting and cannot be
        configured server-wide. The bot is elevated to **host** by the
        policy server's ``participant_properties`` callback, which
        recognises the bot by display name and returns
        ``role: "chair"`` + ``bypass_lock: true``. That makes the bot a
        host as soon as it joins, regardless of the meeting's PIN
        configuration, so it can call ``set_classification_level``. If
        the meeting requires a guest PIN that we don't have, the call
        will fail and the caller logs and moves on.
        """
        url = f"{self._base(conference_alias)}/request_token"
        body = {"display_name": self.display_name}
        headers = {"Content-Type": "application/json"}
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            verify=self.verify_tls,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {}) or {}
        token = result.get("token")
        if not token:
            raise RuntimeError(
                f"request_token returned no token for {conference_alias}: {data}"
            )
        try:
            expires = int(result.get("expires", _DEFAULT_TOKEN_EXPIRES))
        except (TypeError, ValueError):
            expires = _DEFAULT_TOKEN_EXPIRES
        return token, expires

    def refresh_token(self, conference_alias: str, token: str) -> int:
        """Refresh the token. Returns the new ``expires`` in seconds.

        Raises if the conference has ended or the token is no longer valid;
        the caller uses that as the signal to stop keeping the bot alive.
        """
        url = f"{self._base(conference_alias)}/refresh_token"
        data = self._post(url, token, None)
        result = (data or {}).get("result", {}) or {}
        try:
            return int(result.get("expires", _DEFAULT_TOKEN_EXPIRES))
        except (TypeError, ValueError):
            return _DEFAULT_TOKEN_EXPIRES

    def release_token(self, conference_alias: str, token: str) -> None:
        url = f"{self._base(conference_alias)}/release_token"
        try:
            self._post(url, token, None)
        except Exception as exc:  # noqa: BLE001 - cleanup, log and move on
            log.warning("release_token failed for %s: %s", conference_alias, exc)

    # --------------------------------------------------------------- actions
    def set_classification_level(
        self, conference_alias: str, token: str, level: int
    ) -> None:
        url = f"{self._base(conference_alias)}/set_classification_level"
        self._post(url, token, {"classification_level": int(level)})

    def set_elapsed_clock(
        self,
        conference_alias: str,
        token: str,
        prefix: str = "Elapsed: ",
        suffix: str = "",
    ) -> None:
        """Add an elapsed-time timer to the conference stage."""
        url = f"{self._base(conference_alias)}/set_clock"
        self._post(
            url,
            token,
            {"type": "elapsed", "prefix": prefix, "suffix": suffix},
        )

    # -------------------------------------------------------------- workflow
    def apply_classification_and_timer(
        self,
        conference_alias: str,
        classification_level: int,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """Token → classification → elapsed clock → keep-alive loop → release.

        After applying the classification level and the elapsed-time clock,
        the helper does **not** release the token. Instead it loops calling
        ``refresh_token`` so the Policy Server participant stays in the
        meeting for its full duration, regardless of whether the meeting has
        a host PIN. The loop terminates (and the token is released) when
        ``refresh_token`` fails — typically because the conference has
        ended — or when ``stop_event`` is set.

        All exceptions are caught and logged so a Client API outage cannot
        break the policy server's response path.
        """
        try:
            token, expires = self.request_token(conference_alias)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Client API request_token failed for %s: %s",
                conference_alias,
                exc,
            )
            return

        with self._tokens_lock:
            self._tokens[conference_alias] = token
        try:
            try:
                self.set_classification_level(
                    conference_alias, token, classification_level
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "set_classification_level failed for %s: %s",
                    conference_alias,
                    exc,
                )
            try:
                self.set_elapsed_clock(conference_alias, token)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "set_clock (elapsed) failed for %s: %s", conference_alias, exc
                )

            self._keep_alive(conference_alias, token, expires, stop_event)
        finally:
            with self._tokens_lock:
                self._tokens.pop(conference_alias, None)
            self.release_token(conference_alias, token)

    def update_classification_level(
        self, conference_alias: str, level: int
    ) -> bool:
        """Re-apply the classification level for a meeting that is already running.

        Called when a later participant's domain lowers the meeting's
        effective classification (the minimum across all joined
        participants). Re-uses the live Policy Server bot's token so we
        don't spawn a second bot. Returns ``True`` if the update was
        delivered, ``False`` if no live token is available (e.g. the bot
        hasn't joined yet or has already left).
        """
        with self._tokens_lock:
            token = self._tokens.get(conference_alias)
        if not token:
            log.info(
                "No live Policy Server token for %s; cannot push updated "
                "classification level %s yet",
                conference_alias,
                level,
            )
            return False
        try:
            self.set_classification_level(conference_alias, token, int(level))
        except Exception as exc:  # noqa: BLE001 - never bubble into Flask
            log.warning(
                "update_classification_level (set_classification_level) failed "
                "for %s: %s",
                conference_alias,
                exc,
            )
            return False
        log.info(
            "Updated classification level for %s to L%s", conference_alias, level
        )
        return True

    def _keep_alive(
        self,
        conference_alias: str,
        token: str,
        expires: int,
        stop_event: Optional[threading.Event],
    ) -> None:
        """Refresh the token periodically until the conference ends.

        Sleeps for roughly half of the advertised token lifetime (clamped to
        a sane range) between refreshes, so a single missed refresh does not
        immediately drop the bot out of the meeting.
        """
        log.info(
            "Policy Server bot joined %s; keeping token alive until conference ends",
            conference_alias,
        )
        while True:
            # Refresh well before the advertised expiry, but never sleep
            # for less than 15s to avoid hammering the node.
            interval = max(15, min(expires // 2, expires - _REFRESH_SAFETY_MARGIN))
            if stop_event is not None and stop_event.wait(interval):
                log.info("Stop requested; leaving %s", conference_alias)
                return
            elif stop_event is None:
                time.sleep(interval)
            try:
                expires = self.refresh_token(conference_alias, token)
            except Exception as exc:  # noqa: BLE001
                # Most commonly: conference ended → 403/404. Stop keeping
                # the bot alive; the surrounding ``finally`` releases the
                # (now-invalid) token.
                log.info(
                    "refresh_token stopped for %s (%s); bot leaving meeting",
                    conference_alias,
                    exc,
                )
                return
