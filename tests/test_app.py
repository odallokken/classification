"""Tests for the policy server.

Run with::

    pip install -r requirements.txt pytest
    pytest -q
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture
def app(monkeypatch):
    # Use a fresh DB per test, and disable any real Client API calls.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("POLICY_DB_PATH", tmp.name)
    monkeypatch.setenv("ENABLE_CLIENT_API", "false")
    monkeypatch.setenv("PEXIP_NODE", "")
    monkeypatch.setenv("DEFAULT_CLASSIFICATION_LEVEL", "1")

    # Reload modules so they pick up the new environment.
    import importlib

    import config
    import storage
    import app as app_module
    importlib.reload(config)
    importlib.reload(storage)
    importlib.reload(app_module)

    application = app_module.create_app(client_api=None)
    application.config.update(TESTING=True)
    yield application

    os.unlink(tmp.name)


@pytest.fixture
def client(app):
    return app.test_client()


def test_extract_caller_domain():
    from app import _extract_caller_domain

    assert _extract_caller_domain("sip:alice@example.com") == "example.com"
    assert _extract_caller_domain("alice@Example.COM") == "example.com"
    assert _extract_caller_domain("sip:bob@host.example.com:5060") == "host.example.com"
    assert _extract_caller_domain("sip:bob@example.com;transport=tls") == "example.com"
    assert _extract_caller_domain("+15551234567") == ""
    assert _extract_caller_domain("") == ""
    assert _extract_caller_domain(None) == ""


def test_normalize_alias():
    from app import _normalize_alias

    assert _normalize_alias("sip:meeting@conf.example.com") == "meeting"
    assert _normalize_alias("h323:roomA@example.com") == "roomA"
    assert _normalize_alias("plain-alias") == "plain-alias"
    assert _normalize_alias("") == ""


def test_admin_api_crud_and_lookup(client):
    # Initially empty.
    resp = client.get("/api/domains")
    assert resp.status_code == 200
    assert resp.get_json() == []

    # Create.
    resp = client.post(
        "/api/domains",
        json={"domain": "Example.COM", "classification_level": 2, "label": "Official"},
    )
    assert resp.status_code == 201
    assert resp.get_json() == {
        "domain": "example.com",
        "classification_level": 2,
        "label": "Official",
    }

    # List.
    resp = client.get("/api/domains")
    assert resp.status_code == 200
    assert resp.get_json() == [
        {"domain": "example.com", "classification_level": 2, "label": "Official"}
    ]

    # Update (upsert).
    resp = client.post(
        "/api/domains",
        json={"domain": "example.com", "classification_level": 3, "label": "Secret"},
    )
    assert resp.status_code == 201
    assert resp.get_json()["classification_level"] == 3

    # Delete.
    resp = client.delete("/api/domains/example.com")
    assert resp.status_code == 204
    resp = client.delete("/api/domains/example.com")
    assert resp.status_code == 404


def test_service_configuration_classifies_by_domain(client):
    client.post(
        "/api/domains",
        json={"domain": "secure.example.com", "classification_level": 3,
              "label": "Secret"},
    )

    resp = client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:meet1@conf.example.com",
            "remote_alias": "sip:alice@secure.example.com",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["action"] == "continue"
    result = body["result"]
    assert result["service_type"] == "conference"
    assert result["local_alias"] == "meet1"
    assert result["name"] == "meet1"
    assert result["service_tag"] == "classification-l3"
    assert "Secret" in result["description"]
    assert result["view"]  # always set


def test_service_configuration_unknown_domain_uses_default(client):
    resp = client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:meet2@conf.example.com",
            "remote_alias": "sip:bob@unknown.tld",
        },
    )
    assert resp.status_code == 200
    result = resp.get_json()["result"]
    assert result["service_tag"] == "classification-l1"


def test_service_configuration_parent_domain_match(client):
    client.post(
        "/api/domains",
        json={"domain": "example.com", "classification_level": 1, "label": "Official"},
    )
    resp = client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:meet3@conf.example.com",
            "remote_alias": "sip:carol@mail.example.com",
        },
    )
    assert resp.get_json()["result"]["service_tag"] == "classification-l1"


def test_participant_properties_returns_empty_result(client):
    # First, register the conference via a service_configuration request.
    client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:meet4@conf.example.com",
            "remote_alias": "sip:dave@example.com",
        },
    )

    resp = client.post(
        "/policy/v1/participant/properties",
        json={
            "service_name": "meet4",
            "remote_alias": "sip:dave@example.com",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["action"] == "continue"
    assert body["result"] == {}


def test_participant_properties_triggers_client_api_once(monkeypatch, client, app):
    # Replace the client API with a counting stub.
    calls = []

    class StubClient:
        def apply_classification_and_timer(self, alias, level):
            calls.append((alias, level))

    app.config["CLIENT_API"] = StubClient()

    client.post(
        "/api/domains",
        json={"domain": "secret.gov", "classification_level": 4, "label": "TS"},
    )
    client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:tsmeet@conf.example.com",
            "remote_alias": "sip:agent@secret.gov",
        },
    )
    # Two participants joining.
    client.post(
        "/policy/v1/participant/properties",
        json={"service_name": "tsmeet", "remote_alias": "sip:agent@secret.gov"},
    )
    client.post(
        "/policy/v1/participant/properties",
        json={"service_name": "tsmeet", "remote_alias": "sip:other@secret.gov"},
    )

    # Background thread runs synchronously enough for our purposes; give it a moment.
    import time

    for _ in range(20):
        if calls:
            break
        time.sleep(0.05)

    assert calls == [("tsmeet", 4)], calls


def test_participant_properties_elevates_policy_server_bot_to_host(client):
    # Register the conference first so participant_properties is happy.
    client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:hostmeet@conf.example.com",
            "remote_alias": "sip:alice@example.com",
        },
    )

    # The bot joins via the Client API with display_name "Policy Server",
    # which Pexip surfaces as the participant's remote_alias on the
    # policy callback.
    resp = client.post(
        "/policy/v1/participant/properties",
        json={
            "service_name": "hostmeet",
            "remote_alias": "Policy Server",
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["action"] == "continue"
    # Must always be host, regardless of meeting PIN/lock state, so the
    # bot can call set_classification_level via the Client API.
    assert body["result"] == {"role": "chair", "bypass_lock": True}


def test_participant_properties_bot_match_is_case_insensitive(client):
    client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:hostmeet2@conf.example.com",
            "remote_alias": "sip:alice@example.com",
        },
    )
    resp = client.post(
        "/policy/v1/participant/properties",
        json={
            "service_name": "hostmeet2",
            "remote_display_name": "policy server",
        },
    )
    assert resp.get_json()["result"] == {"role": "chair", "bypass_lock": True}


def test_admin_api_rejects_levels_outside_one_to_five(client):
    # Below the allowed range.
    resp = client.post(
        "/api/domains",
        json={"domain": "low.example.com", "classification_level": 0},
    )
    assert resp.status_code == 400
    assert "between 1 and 5" in resp.get_json()["error"]

    # Above the allowed range.
    resp = client.post(
        "/api/domains",
        json={"domain": "high.example.com", "classification_level": 6},
    )
    assert resp.status_code == 400
    assert "between 1 and 5" in resp.get_json()["error"]

    # Boundary values are accepted.
    for lvl in (1, 5):
        resp = client.post(
            "/api/domains",
            json={
                "domain": f"ok{lvl}.example.com",
                "classification_level": lvl,
            },
        )
        assert resp.status_code == 201, resp.get_json()


def test_second_participant_lowers_meeting_classification(client, app):
    # Two mapped domains: trusted (L4) and untrusted (L2).
    client.post(
        "/api/domains",
        json={"domain": "trusted.example.com", "classification_level": 4,
              "label": "Secret"},
    )
    client.post(
        "/api/domains",
        json={"domain": "guest.example.com", "classification_level": 2,
              "label": "Official"},
    )

    # Stub the Client API so we can verify update_classification_level gets
    # called when the lower-level participant joins. Also simulate that the
    # bot already holds a live token by populating _tokens directly.
    update_calls: list = []
    apply_calls: list = []

    class StubClient:
        def __init__(self):
            # Mirror the real PexipClientAPI surface used by app.py.
            self._tokens = {}

        def apply_classification_and_timer(self, alias, level):
            apply_calls.append((alias, level))
            # Simulate the bot having a live token after joining.
            self._tokens[alias] = "live-token"

        def update_classification_level(self, alias, level):
            update_calls.append((alias, level))
            return True

    app.config["CLIENT_API"] = StubClient()

    # Meeting is created by the trusted caller — initial level L4.
    resp = client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:mixmeet@conf.example.com",
            "remote_alias": "sip:alice@trusted.example.com",
        },
    )
    assert resp.get_json()["result"]["service_tag"] == "classification-l4"

    # First participant (trusted): triggers the Client API apply path.
    client.post(
        "/policy/v1/participant/properties",
        json={
            "service_name": "mixmeet",
            "remote_alias": "sip:alice@trusted.example.com",
        },
    )

    import time

    for _ in range(20):
        if apply_calls:
            break
        time.sleep(0.05)
    assert apply_calls == [("mixmeet", 4)]

    # Second participant from a lower-classification domain joins. The
    # meeting's effective level should drop from L4 to L2 and the policy
    # server should push the new level via the Client API.
    client.post(
        "/policy/v1/participant/properties",
        json={
            "service_name": "mixmeet",
            "remote_alias": "sip:eve@guest.example.com",
        },
    )

    for _ in range(20):
        if update_calls:
            break
        time.sleep(0.05)
    assert update_calls == [("mixmeet", 2)]

    # The stored conference level reflects the lowered classification.
    import storage as storage_module
    from config import settings as live_settings

    level, _ = storage_module.get_conference_state(
        live_settings.database_path, "mixmeet"
    )
    assert level == 2


def test_higher_classification_participant_does_not_raise_meeting_level(
    client, app
):
    client.post(
        "/api/domains",
        json={"domain": "trusted.example.com", "classification_level": 4},
    )
    client.post(
        "/api/domains",
        json={"domain": "supertrusted.example.com", "classification_level": 5},
    )

    update_calls: list = []

    class StubClient:
        def __init__(self):
            self._tokens = {"raisemeet": "live-token"}

        def apply_classification_and_timer(self, alias, level):
            pass

        def update_classification_level(self, alias, level):
            update_calls.append((alias, level))
            return True

    app.config["CLIENT_API"] = StubClient()

    # Meeting starts at L4.
    client.post(
        "/policy/v1/service/configuration",
        json={
            "local_alias": "sip:raisemeet@conf.example.com",
            "remote_alias": "sip:alice@trusted.example.com",
        },
    )
    client.post(
        "/policy/v1/participant/properties",
        json={
            "service_name": "raisemeet",
            "remote_alias": "sip:alice@trusted.example.com",
        },
    )

    # A more-trusted (higher-level) participant joins — the meeting's
    # classification must NOT silently rise.
    client.post(
        "/policy/v1/participant/properties",
        json={
            "service_name": "raisemeet",
            "remote_alias": "sip:vip@supertrusted.example.com",
        },
    )

    import time
    time.sleep(0.15)

    assert update_calls == []

    import storage as storage_module
    from config import settings as live_settings

    level, _ = storage_module.get_conference_state(
        live_settings.database_path, "raisemeet"
    )
    assert level == 4


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_admin_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Domain" in resp.data
    assert b"Classification" in resp.data
