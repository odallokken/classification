"""Runtime configuration for the policy server.

All settings are read from environment variables so the service can be
deployed without code changes. Sensible defaults are provided for local
development.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Storage
    database_path: str = os.environ.get(
        "POLICY_DB_PATH", os.path.join(os.path.dirname(__file__), "policy.db")
    )

    # Default classification level when a caller's domain has no mapping.
    default_classification_level: int = _env_int("DEFAULT_CLASSIFICATION_LEVEL", 0)

    # Pexip Client API settings (required to apply classification + timer)
    pexip_node: str = os.environ.get("PEXIP_NODE", "")  # e.g. "conf.example.com"
    pexip_display_name: str = os.environ.get(
        "PEXIP_PS_DISPLAY_NAME", "Policy Server"
    )
    pexip_pin: str = os.environ.get("PEXIP_HOST_PIN", "")
    pexip_verify_tls: bool = _env_bool("PEXIP_VERIFY_TLS", True)
    pexip_request_timeout: int = _env_int("PEXIP_HTTP_TIMEOUT", 10)

    # Whether the Client API helper should run. Useful to disable in tests.
    enable_client_api: bool = _env_bool("ENABLE_CLIENT_API", True)


settings = Settings()
