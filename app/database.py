"""SQLite persistence layer.

Uses Python's built-in :mod:`sqlite3` (no external DB dependency) so the tool
is trivial to run and test. The database path can be overridden with the
``TRIAGEBOT_DB`` environment variable, which the test-suite uses to point at a
throwaway file.

Schema changes are applied as numbered migrations tracked with SQLite's
``PRAGMA user_version``. Each migration runs once, inside a transaction, so an
existing database is upgraded in place without losing data.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "triagebot.db")

# (version, SQL). Never edit a released migration; append a new one instead.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- Manually triaged issues (the original TriageBot feature).
        CREATE TABLE IF NOT EXISTS issues (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            body        TEXT    NOT NULL DEFAULT '',
            severity    TEXT    NOT NULL,
            labels      TEXT    NOT NULL DEFAULT '',   -- comma-separated
            owner       TEXT    NOT NULL,
            reason      TEXT    NOT NULL DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'open', -- open | closed
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    (
        2,
        """
        -- GitHub repositories that have been imported.
        CREATE TABLE repos (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name         TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- owner/name
            html_url          TEXT    NOT NULL,
            description       TEXT    NOT NULL DEFAULT '',
            last_imported_at  TEXT
        );

        -- Issues and pull requests imported from GitHub, with triage results.
        CREATE TABLE items (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id              INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            number               INTEGER NOT NULL,
            kind                 TEXT    NOT NULL CHECK (kind IN ('issue', 'pr')),
            title                TEXT    NOT NULL,
            body                 TEXT    NOT NULL DEFAULT '',
            state                TEXT    NOT NULL CHECK (state IN ('open', 'closed')),
            author               TEXT    NOT NULL DEFAULT '',
            html_url             TEXT    NOT NULL,
            github_labels        TEXT    NOT NULL DEFAULT '[]',  -- JSON array
            comments             INTEGER NOT NULL DEFAULT 0,
            reactions            INTEGER NOT NULL DEFAULT 0,
            is_draft             INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT    NOT NULL,
            updated_at           TEXT    NOT NULL,
            closed_at            TEXT,
            category             TEXT    NOT NULL,
            category_confidence  TEXT    NOT NULL,
            priority             TEXT    NOT NULL,
            owner_hint           TEXT,
            category_reasons     TEXT    NOT NULL DEFAULT '[]',  -- JSON array
            priority_reasons     TEXT    NOT NULL DEFAULT '[]',  -- JSON array
            imported_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (repo_id, number)
        );

        CREATE INDEX idx_items_repo_updated ON items (repo_id, updated_at);
        CREATE INDEX idx_items_filters ON items (kind, category, priority, state);
        """,
    ),
    (
        3,
        """
        -- Phase 2: where each item's category came from, and the ML probability.
        ALTER TABLE items ADD COLUMN category_source TEXT NOT NULL DEFAULT 'rules';
        ALTER TABLE items ADD COLUMN category_score REAL;
        """,
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _db_path() -> str:
    return os.environ.get("TRIAGEBOT_DB", DEFAULT_DB_PATH)


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with row access by column name."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_schema_version() -> int:
    with get_connection() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def init_db() -> None:
    """Apply any migrations newer than the database's ``user_version``."""
    with get_connection() as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            # executescript() commits first, so wrap each migration explicitly;
            # user_version is part of the transaction, keeping both atomic.
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
