"""SQLite-backed storage for domain → classification mappings and per-conference state.

Two tables:

* ``domain_classification`` — administrator-editable mapping of caller domain
  (e.g. ``"example.com"``) to integer classification level (0..n).
* ``conference_state`` — per-conference flag tracking whether the Client API
  side-effects (``set_classification_level`` + elapsed ``set_clock``) have
  already been applied. The ``UNIQUE`` constraint on ``conference_alias``
  acts as a cross-process atomic gate (see the ``pexip-policy-server`` skill,
  SS4, "Cross-Worker Dedup").
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

_LOCK = threading.Lock()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str) -> None:
    """Create tables if they do not yet exist. Safe to call repeatedly."""
    with _LOCK, _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS domain_classification (
                domain               TEXT PRIMARY KEY,
                classification_level INTEGER NOT NULL,
                label                TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conference_state (
                conference_alias     TEXT PRIMARY KEY,
                classification_level INTEGER NOT NULL,
                applied              INTEGER NOT NULL DEFAULT 0
            )
            """
        )


@contextmanager
def _cursor(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Domain → classification mappings (administrator-editable)
# ---------------------------------------------------------------------------


def list_domains(db_path: str) -> List[Tuple[str, int, Optional[str]]]:
    with _cursor(db_path) as conn:
        rows = conn.execute(
            "SELECT domain, classification_level, label "
            "FROM domain_classification ORDER BY domain"
        ).fetchall()
    return [(r["domain"], r["classification_level"], r["label"]) for r in rows]


def upsert_domain(
    db_path: str, domain: str, level: int, label: Optional[str] = None
) -> None:
    domain = domain.strip().lower()
    if not domain:
        raise ValueError("domain must not be empty")
    if level < 0:
        raise ValueError("classification_level must be >= 0")
    with _cursor(db_path) as conn:
        conn.execute(
            """
            INSERT INTO domain_classification (domain, classification_level, label)
            VALUES (?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                classification_level = excluded.classification_level,
                label                = excluded.label
            """,
            (domain, int(level), label),
        )


def delete_domain(db_path: str, domain: str) -> bool:
    domain = domain.strip().lower()
    with _cursor(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM domain_classification WHERE domain = ?", (domain,)
        )
        return cur.rowcount > 0


def lookup_classification(
    db_path: str, domain: str, default_level: int
) -> Tuple[int, Optional[str]]:
    """Return (level, label) for the domain, or (default_level, None) if unknown.

    Falls back to a parent-domain match (``mail.example.com`` → ``example.com``)
    so administrators can configure broad rules without enumerating subdomains.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return default_level, None
    with _cursor(db_path) as conn:
        # Exact match first.
        row = conn.execute(
            "SELECT classification_level, label "
            "FROM domain_classification WHERE domain = ?",
            (domain,),
        ).fetchone()
        if row is not None:
            return row["classification_level"], row["label"]
        # Parent-domain match, longest-suffix wins.
        parts = domain.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[i:])
            row = conn.execute(
                "SELECT classification_level, label "
                "FROM domain_classification WHERE domain = ?",
                (parent,),
            ).fetchone()
            if row is not None:
                return row["classification_level"], row["label"]
    return default_level, None


# ---------------------------------------------------------------------------
# Per-conference state (atomic "have we applied side-effects yet?" gate)
# ---------------------------------------------------------------------------


def claim_conference(
    db_path: str, conference_alias: str, classification_level: int
) -> bool:
    """Atomically claim the right to apply Client-API side-effects for this VMR.

    Returns ``True`` if this caller is the first (and should run the Client
    API actions). Returns ``False`` if another worker/thread already claimed
    it (idempotent — caller should skip).
    """
    try:
        with _cursor(db_path) as conn:
            conn.execute(
                "INSERT INTO conference_state "
                "(conference_alias, classification_level, applied) VALUES (?, ?, 0)",
                (conference_alias, int(classification_level)),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def mark_conference_applied(db_path: str, conference_alias: str) -> None:
    with _cursor(db_path) as conn:
        conn.execute(
            "UPDATE conference_state SET applied = 1 WHERE conference_alias = ?",
            (conference_alias,),
        )


def reset_conference(db_path: str, conference_alias: str) -> None:
    """Remove a conference from the state table (used on conference end)."""
    with _cursor(db_path) as conn:
        conn.execute(
            "DELETE FROM conference_state WHERE conference_alias = ?",
            (conference_alias,),
        )


def get_conference_state(
    db_path: str, conference_alias: str
) -> Optional[Tuple[int, bool]]:
    with _cursor(db_path) as conn:
        row = conn.execute(
            "SELECT classification_level, applied "
            "FROM conference_state WHERE conference_alias = ?",
            (conference_alias,),
        ).fetchone()
    if row is None:
        return None
    return row["classification_level"], bool(row["applied"])
