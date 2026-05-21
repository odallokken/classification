"""Minimal Pexip Infinity Client API helper.

Used by the policy server to attach the two side-effects that **must** be
delivered through the Client API:

1. ``set_classification_level`` — applies the meeting-wide classification
   level computed from the caller's domain.
2. ``set_clock`` with ``type: "elapsed"`` — adds an elapsed-time conference
   timer that is visible to every participant.

The helper opens a short-lived host token (the "Policy Server" participant
documented in the ``pexip-policy-server`` skill, SS4), issues the two POSTs,
then releases the token. Errors are logged but never raised back to the
policy request — Pexip must always receive its policy response on time.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)


class PexipClientAPI:
    """Thin wrapper over the Pexip Client REST API.

    Parameters
    ----------
    node:
        Hostname or IP of a Pexip Conferencing Node (e.g. ``"conf.example.com"``).
    display_name:
        Display name used when joining as the Policy Server participant.
    pin:
        Host PIN for the conference, if one is configured. Empty string means
        "no PIN" — Pexip then accepts the token request without a PIN.
    verify_tls:
        Whether to verify the node's TLS certificate. Defaults to ``True``.
    timeout:
        HTTP timeout in seconds.
    """

    def __init__(
        self,
        node: str,
        display_name: str = "Policy Server",
        pin: str = "",
        verify_tls: bool = True,
        timeout: int = 10,
    ) -> None:
        self.node = node
        self.display_name = display_name
        self.pin = pin
        self.verify_tls = verify_tls
        self.timeout = timeout

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
    def request_token(self, conference_alias: str) -> str:
        """Request a host token. Returns the token string."""
        url = f"{self._base(conference_alias)}/request_token"
        body = {"display_name": self.display_name}
        headers = {"Content-Type": "application/json"}
        if self.pin:
            headers["pin"] = self.pin
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            verify=self.verify_tls,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("result", {}).get("token")
        if not token:
            raise RuntimeError(
                f"request_token returned no token for {conference_alias}: {data}"
            )
        return token

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
        self, conference_alias: str, classification_level: int
    ) -> None:
        """One-shot helper: token → classification → elapsed clock → release.

        All exceptions are caught and logged so a Client API outage cannot
        break the policy server's response path.
        """
        token: Optional[str] = None
        try:
            token = self.request_token(conference_alias)
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
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Client API request_token failed for %s: %s",
                conference_alias,
                exc,
            )
        finally:
            if token is not None:
                self.release_token(conference_alias, token)
