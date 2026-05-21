"""Flask application: Pexip Infinity external policy server + admin UX.

Endpoints
---------

Policy (called by Pexip Infinity)

* ``POST /policy/v1/service/configuration`` — classify the VMR by caller
  domain and return a valid service_configuration response.
* ``POST /policy/v1/participant/properties`` — passthrough for participant
  properties; also the trigger that fires the Client API side-effects
  (classification level + elapsed timer) once per conference.

Admin UX (used by humans)

* ``GET  /``                  — HTML page listing/editing domain mappings.
* ``GET  /api/domains``       — JSON list of mappings.
* ``POST /api/domains``       — create or update a mapping.
* ``DELETE /api/domains/<d>`` — remove a mapping.

The policy response shapes follow the conventions in
``sidumorjens/pexip-claude-skills`` (skills ``pexip-external-policy`` and
``pexip-policy-server``):

* Successful service configurations are wrapped in
  ``{"status": "success", "action": "continue", "result": {...}}``.
* The ``result`` always includes ``service_type``, ``name``, ``local_alias``,
  ``service_tag`` and a normalized ``view`` value.
* Participant properties uses the same envelope and never hard-codes role
  overrides — it returns an empty result, which tells Pexip to use its own
  defaults.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request

import storage
from config import settings
from pexip_client import PexipClientAPI

log = logging.getLogger(__name__)

# The valid Pexip ``view`` values (see service_configuration reference in
# the pexip-external-policy / pexip-policy-server skills). An invalid value
# rejects the entire policy response, so always normalize through this set.
_VALID_VIEWS = {
    "one_main_zero_pips",
    "one_main_seven_pips",
    "one_main_twentyone_pips",
    "two_mains_twentyone_pips",
    "four_mains_zero_pips",
    "five_mains_seven_pips",
    "nine_mains_zero_pips",
    "sixteen_mains_zero_pips",
    "twentyfive_mains_zero_pips",
    "self_view",
    "speaker_only",
    "ac_presentation_in_mix",
    "ac",
    "default",
}
_DEFAULT_VIEW = "one_main_twentyone_pips"


def _normalize_alias(alias: Optional[str]) -> str:
    """Strip ``sip:``/``h323:`` prefix and ``@domain`` suffix from a Pexip alias.

    SIP calls arrive with ``local_alias`` of the form
    ``sip:meeting@conf.example.com``. The bare alias is what Pexip uses as
    the conference identifier.
    """
    if not alias:
        return ""
    a = alias.strip()
    for prefix in ("sip:", "sips:", "h323:", "tel:"):
        if a.lower().startswith(prefix):
            a = a[len(prefix):]
            break
    if "@" in a:
        a = a.split("@", 1)[0]
    return a


def _extract_caller_domain(remote_alias: Optional[str]) -> str:
    """Return the domain part of the caller's ``remote_alias``.

    ``sip:alice@example.com``  → ``example.com``
    ``alice@example.com``      → ``example.com``
    ``+15551234`` (no domain)  → ``""``
    """
    if not remote_alias:
        return ""
    a = remote_alias.strip()
    for prefix in ("sip:", "sips:", "h323:", "tel:"):
        if a.lower().startswith(prefix):
            a = a[len(prefix):]
            break
    if "@" not in a:
        return ""
    domain = a.split("@", 1)[1]
    # Trim port / parameters: alice@example.com:5060;transport=tls
    for sep in (":", ";", ">"):
        if sep in domain:
            domain = domain.split(sep, 1)[0]
    return domain.strip().lower()


def _normalize_view(view: Optional[str]) -> str:
    if view and view in _VALID_VIEWS:
        return view
    return _DEFAULT_VIEW


def _is_policy_server_participant(params: dict) -> bool:
    """Return True if the participant in ``params`` is this server's bot.

    The Policy Server bot joins via the Client API with ``display_name``
    set to ``settings.pexip_display_name`` (default ``"Policy Server"``).
    Pexip populates ``remote_alias`` and ``remote_display_name`` on the
    policy callback from that field for participants that have no
    SIP/H.323 URI, so matching either against the configured display
    name is sufficient.
    """
    expected = (settings.pexip_display_name or "").strip().lower()
    if not expected:
        return False
    for key in ("remote_alias", "remote_display_name", "display_name"):
        value = params.get(key)
        if value and str(value).strip().lower() == expected:
            return True
    return False


def _request_payload() -> dict:
    """Return the request body as a dict whether sent as JSON, form, or query.

    Pexip's policy requests are documented as GET with query parameters, but
    different field deployments POST JSON or form bodies. Accept all three
    so the server is tolerant to the configured calling convention.
    """
    if request.is_json:
        return request.get_json(silent=True) or {}
    if request.form:
        return request.form.to_dict(flat=True)
    if request.args:
        return request.args.to_dict(flat=True)
    return {}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(client_api: Optional[PexipClientAPI] = None) -> Flask:
    """Build the Flask app. Tests pass a stub ``client_api`` to avoid network."""
    app = Flask(__name__, template_folder="templates", static_folder="static")

    storage.init_db(settings.database_path)

    if client_api is None and settings.enable_client_api and settings.pexip_node:
        client_api = PexipClientAPI(
            node=settings.pexip_node,
            display_name=settings.pexip_display_name,
            verify_tls=settings.pexip_verify_tls,
            timeout=settings.pexip_request_timeout,
        )
    app.config["CLIENT_API"] = client_api

    # ----------------------------------------------------- policy endpoints

    @app.route("/policy/v1/service/configuration", methods=["GET", "POST"])
    def service_configuration():  # noqa: D401 - Flask view
        params = _request_payload()
        local_alias = _normalize_alias(params.get("local_alias"))
        remote_alias = params.get("remote_alias", "")
        caller_domain = _extract_caller_domain(remote_alias)

        level, label = storage.lookup_classification(
            settings.database_path,
            caller_domain,
            settings.default_classification_level,
        )

        log.info(
            "service_configuration alias=%s caller_domain=%s -> level=%s (%s)",
            local_alias,
            caller_domain,
            level,
            label,
        )

        # Remember the level for this VMR so the participant_properties hook
        # can apply it via the Client API once the first participant joins.
        # ``claim_conference`` is idempotent on conflict.
        if local_alias:
            storage.claim_conference(settings.database_path, local_alias, level)

        result = {
            "service_type": "conference",
            "name": local_alias or "vmr",
            "local_alias": local_alias,
            "service_tag": f"classification-l{level}",
            "view": _normalize_view(None),
            "description": (
                f"Classification: {label} (L{level})"
                if label
                else f"Classification: L{level}"
            ),
        }
        return jsonify({"status": "success", "action": "continue", "result": result})

    @app.route("/policy/v1/participant/properties", methods=["GET", "POST"])
    def participant_properties():  # noqa: D401 - Flask view
        params = _request_payload()
        local_alias = _normalize_alias(
            params.get("service_name") or params.get("local_alias")
        )

        if local_alias:
            _maybe_apply_client_api_actions(app, local_alias)

        # Always elevate the Policy Server bot to host (chair) so it can
        # call ``set_classification_level`` regardless of whether the
        # meeting has a host PIN, allows guests, or is locked. Detected
        # by matching the participant's ``remote_alias`` against the
        # configured Policy Server display name (Pexip populates
        # ``remote_alias`` from the Client API ``display_name`` for
        # participants that have no SIP/H.323 URI). This is the
        # canonical pattern documented in the ``pexip-policy-server``
        # skill (SS2 Role Assignment Ladder, gotcha #12) and must be
        # checked **before** any other role logic.
        if _is_policy_server_participant(params):
            return jsonify(
                {
                    "status": "success",
                    "action": "continue",
                    "result": {"role": "chair", "bypass_lock": True},
                }
            )

        # Empty result tells Pexip "use defaults" — we deliberately do
        # not override roles for real participants here.
        return jsonify({"status": "success", "action": "continue", "result": {}})

    # --------------------------------------------------------- admin UX

    @app.get("/")
    def admin_page():  # noqa: D401
        rows = storage.list_domains(settings.database_path)
        return render_template(
            "admin.html",
            mappings=rows,
            default_level=settings.default_classification_level,
        )

    @app.get("/api/domains")
    def list_domains_api():  # noqa: D401
        rows = storage.list_domains(settings.database_path)
        return jsonify(
            [
                {"domain": d, "classification_level": lvl, "label": lbl}
                for (d, lvl, lbl) in rows
            ]
        )

    @app.post("/api/domains")
    def upsert_domain_api():  # noqa: D401
        body = request.get_json(silent=True) or request.form.to_dict()
        domain = (body.get("domain") or "").strip().lower()
        label = (body.get("label") or "").strip() or None
        try:
            level = int(body.get("classification_level"))
        except (TypeError, ValueError):
            return jsonify({"error": "classification_level must be an integer"}), 400
        if not domain:
            return jsonify({"error": "domain is required"}), 400
        if level < 0:
            return jsonify({"error": "classification_level must be >= 0"}), 400
        storage.upsert_domain(settings.database_path, domain, level, label)
        return (
            jsonify(
                {"domain": domain, "classification_level": level, "label": label}
            ),
            201,
        )

    @app.delete("/api/domains/<path:domain>")
    def delete_domain_api(domain: str):  # noqa: D401
        deleted = storage.delete_domain(settings.database_path, domain)
        if not deleted:
            abort(404)
        return ("", 204)

    @app.get("/healthz")
    def healthz():  # noqa: D401
        return jsonify({"status": "ok"})

    return app


# ---------------------------------------------------------------------------
# Client API trigger
# ---------------------------------------------------------------------------


def _maybe_apply_client_api_actions(app: Flask, conference_alias: str) -> None:
    """Fire ``set_classification_level`` + elapsed ``set_clock`` once per VMR
    and keep the Policy Server bot in the meeting until it ends.

    Uses the ``conference_state`` table as a cross-process atomic gate so
    multiple Gunicorn workers cannot race and produce duplicate Policy
    Server participants (gotchas #8 and #9 in the ``pexip-policy-server``
    skill). Runs in a daemon thread so the policy response is not delayed
    and so the keep-alive loop can outlive a single Flask request.
    """
    state = storage.get_conference_state(settings.database_path, conference_alias)
    if state is None:
        # service_configuration hasn't been called for this VMR yet.
        return
    level, applied = state
    if applied:
        return

    client_api: Optional[PexipClientAPI] = app.config.get("CLIENT_API")
    if client_api is None:
        log.debug(
            "Client API disabled; skipping classification+timer for %s",
            conference_alias,
        )
        return

    # Mark as applied BEFORE running so concurrent requests don't double-fire.
    storage.mark_conference_applied(settings.database_path, conference_alias)

    def _run() -> None:
        try:
            client_api.apply_classification_and_timer(conference_alias, level)
        except Exception:  # noqa: BLE001 - never bubble into Flask thread
            log.exception("Client API actions failed for %s", conference_alias)

    threading.Thread(
        target=_run,
        name=f"pexip-clientapi-{conference_alias}",
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# WSGI entry point
# ---------------------------------------------------------------------------


app = create_app()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app.run(host="0.0.0.0", port=8080)
