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
    monkeypatch.setenv("DEFAULT_CLASSIFICATION_LEVEL", "0")

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
    assert result["service_tag"] == "classification-l0"


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


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_admin_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Domain" in resp.data
    assert b"Classification" in resp.data
