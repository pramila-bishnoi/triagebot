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
import time
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
    (
        4,
        """
        -- Phase 3: who set the priority. 'maintainer_label' = an explicit priority
        -- label on GitHub (a human decision, kept as-is); 'rules' = a suggestion.
        -- Existing rows were scored by rules, so the default is correct for them.
        ALTER TABLE items ADD COLUMN priority_source TEXT NOT NULL DEFAULT 'rules';
        """,
    ),
    (
        5,
        """
        -- Phase 4: possible duplicate pairs within one repository. Each pair is
        -- stored once (item_a < item_b). 'status' is the maintainer's review:
        -- pending (not reviewed), confirmed, or rejected. Re-analysis never
        -- changes a confirmed/rejected status.
        CREATE TABLE duplicate_suggestions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id           INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            item_a            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            item_b            INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            score             REAL    NOT NULL,
            title_similarity  REAL    NOT NULL,
            body_similarity   REAL,
            shared_terms      TEXT    NOT NULL DEFAULT '[]',  -- JSON array
            status            TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'confirmed', 'rejected')),
            created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            decided_at        TEXT,
            CHECK (item_a < item_b),
            UNIQUE (item_a, item_b)
        );
        CREATE INDEX idx_dup_item_b ON duplicate_suggestions (item_b);
        CREATE INDEX idx_dup_repo ON duplicate_suggestions (repo_id, status);

        -- Skip re-analysis when a repository's issues have not changed.
        ALTER TABLE repos ADD COLUMN duplicates_fingerprint TEXT;
        ALTER TABLE repos ADD COLUMN duplicates_analyzed_at TEXT;
        """,
    ),
    (
        6,
        """
        -- Phase 5: pull-request metadata used for PR insights. One row per PR item.
        -- NULL means "not fetched / not available", never zero.
        CREATE TABLE pull_requests (
            item_id                    INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
            merged_at                  TEXT,     -- from the import (issues API)
            head_ref                   TEXT,     -- from the pulls list
            base_ref                   TEXT,
            author_association         TEXT,
            requested_reviewers        INTEGER,  -- pending review requests (people)
            requested_teams            INTEGER,  -- pending review requests (teams)
            list_synced_at             TEXT,
            additions                  INTEGER,  -- from GET /pulls/{n} (on demand)
            deletions                  INTEGER,
            changed_files              INTEGER,
            commits                    INTEGER,
            review_comments            INTEGER,
            reviews_total              INTEGER,  -- from GET /pulls/{n}/reviews (on demand)
            reviews_approved           INTEGER,  -- reviewers whose latest review approves
            reviews_changes_requested  INTEGER,  -- reviewers whose latest review requests changes
            reviews_commented          INTEGER,
            details_fetched_at         TEXT,
            details_for_updated_at     TEXT      -- the PR's updated_at when details were fetched
        );
        """,
    ),
    (
        7,
        """
        -- Phase 6: issue/PR state synchronisation.
        -- synced_through: a GitHub timestamp T such that every stored item of the
        -- repository is known to be current as of T (anything changed on GitHub
        -- later has updated_at >= T, so "since=T" finds it). NULL = unknown.
        ALTER TABLE repos ADD COLUMN synced_through TEXT;
        ALTER TABLE repos ADD COLUMN last_synced_at TEXT;   -- last successful sync
        """,
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def _db_path() -> str:
    return os.environ.get("TRIAGEBOT_DB", DEFAULT_DB_PATH)


# Seconds a connection waits for another writer's lock before raising
# "database is locked". Writes are short (imports fetch first, then write once).
BUSY_TIMEOUT = 15.0


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with row access by column name.

    One ``with`` block is one transaction: it commits if the block succeeds and
    rolls back if it raises, so a failed operation never leaves partial writes.
    """
    conn = sqlite3.connect(_db_path(), timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_schema_version() -> int:
    with get_connection() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _statements(sql: str) -> Iterator[str]:
    """Split a migration script into complete SQL statements."""
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip() and not all(
        part.strip().startswith("--") or not part.strip() for part in buffer.splitlines()
    ):
        raise ValueError(f"incomplete SQL statement in migration: {buffer.strip()[:80]}")


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch to write-ahead logging, so readers continue while one request writes.

    The mode is stored in the database file, so after the first start this is a
    no-op. Changing it needs an exclusive lock, and SQLite reports "locked"
    *immediately* (without waiting) when another process starting at the same
    moment holds a lock, so retry for up to ``BUSY_TIMEOUT`` seconds.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def init_db() -> None:
    """Apply any migrations newer than the database's ``user_version``.

    Safe when several processes start at once (e.g. ``uvicorn --workers 2``):
    each migration runs in a ``BEGIN IMMEDIATE`` transaction (one writer at a
    time) and re-reads ``user_version`` inside it, so a migration another
    process already applied is skipped instead of failing half-way. The schema
    change and the version bump commit together.
    """
    conn = sqlite3.connect(_db_path(), timeout=BUSY_TIMEOUT, isolation_level=None)
    try:
        _enable_wal(conn)
        for version, sql in MIGRATIONS:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                    conn.execute("ROLLBACK")
                    continue
                for statement in _statements(sql):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {int(version)}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
