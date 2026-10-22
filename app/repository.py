"""Data-access functions.

Thin wrappers around SQL so the route handlers stay small and the queries live
in one place. Manually triaged issue labels are stored comma-separated; lists
on imported items are stored as JSON.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import get_connection

# --- Manually triaged issues ----------------------------------------------


def _labels_to_str(labels: list[str]) -> str:
    return ",".join(labels)


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.get("labels") or ""
    data["labels"] = [label for label in raw.split(",") if label]
    return data


def create_issue(
    title: str, body: str, severity: str, labels: list[str], owner: str, reason: str
) -> dict:
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO issues (title, body, severity, labels, owner, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, body, severity, _labels_to_str(labels), owner, reason),
        )
        issue_id = cursor.lastrowid
    return get_issue(issue_id)  # type: ignore[return-value]


def list_issues() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM issues ORDER BY id DESC").fetchall()
    return [_row_to_dict(row) for row in rows]


def get_issue(issue_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return _row_to_dict(row) if row else None


def update_issue(
    issue_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    severity: str | None = None,
    labels: list[str] | None = None,
    owner: str | None = None,
    reason: str | None = None,
    status: str | None = None,
) -> dict | None:
    fields: dict[str, object] = {}
    if title is not None:
        fields["title"] = title
    if body is not None:
        fields["body"] = body
    if severity is not None:
        fields["severity"] = severity
    if labels is not None:
        fields["labels"] = _labels_to_str(labels)
    if owner is not None:
        fields["owner"] = owner
    if reason is not None:
        fields["reason"] = reason
    if status is not None:
        fields["status"] = status

    if not fields:
        return get_issue(issue_id)

    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [issue_id]
    with get_connection() as conn:
        conn.execute(f"UPDATE issues SET {assignments} WHERE id = ?", values)
    return get_issue(issue_id)


def delete_issue(issue_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM issues WHERE id = ?", (issue_id,))
        deleted = cursor.rowcount > 0
    return deleted


# --- Imported GitHub repositories and items --------------------------------

_JSON_COLUMNS = ("github_labels", "category_reasons", "priority_reasons")

_ITEM_COLUMNS = (
    "number",
    "kind",
    "title",
    "body",
    "state",
    "author",
    "html_url",
    "github_labels",
    "comments",
    "reactions",
    "is_draft",
    "created_at",
    "updated_at",
    "closed_at",
    "category",
    "category_confidence",
    "priority",
    "owner_hint",
    "category_reasons",
    "priority_reasons",
    "category_source",
    "category_score",
    "priority_source",
)

# Whitelisted filters for list_items(): name -> SQL column.
_ITEM_FILTERS = {
    "kind": "i.kind",
    "category": "i.category",
    "priority": "i.priority",
    "state": "i.state",
    "repo": "r.full_name",
    "source": "i.category_source",
}


def _upsert_repo(conn: sqlite3.Connection, full_name: str, html_url: str, description: str) -> int:
    conn.execute(
        """
        INSERT INTO repos (full_name, html_url, description, last_imported_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT (full_name) DO UPDATE SET
            full_name = excluded.full_name,
            html_url = excluded.html_url,
            description = excluded.description,
            last_imported_at = excluded.last_imported_at
        """,
        (full_name, html_url, description),
    )
    return conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()["id"]


def upsert_repo(full_name: str, html_url: str, description: str) -> dict:
    with get_connection() as conn:
        repo_id = _upsert_repo(conn, full_name, html_url, description)
    return get_repo(repo_id)  # type: ignore[return-value]


def get_repo(repo_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT r.*, (SELECT COUNT(*) FROM items WHERE repo_id = r.id) AS item_count
            FROM repos r WHERE r.id = ?
            """,
            (repo_id,),
        ).fetchone()
    return dict(row) if row else None


def list_repos() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, (SELECT COUNT(*) FROM items WHERE repo_id = r.id) AS item_count
            FROM repos r ORDER BY r.full_name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _upsert_items(conn: sqlite3.Connection, repo_id: int, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    columns = ", ".join(_ITEM_COLUMNS)
    placeholders = ", ".join("?" for _ in _ITEM_COLUMNS)
    updates = ", ".join(f"{col} = excluded.{col}" for col in _ITEM_COLUMNS if col != "number")
    sql = f"""
        INSERT INTO items (repo_id, {columns}) VALUES (?, {placeholders})
        ON CONFLICT (repo_id, number) DO UPDATE SET {updates}, imported_at = datetime('now')
    """
    rows = []
    for item in items:
        values = []
        for col in _ITEM_COLUMNS:
            value = item.get(col)
            if col in _JSON_COLUMNS:
                value = json.dumps(value or [])
            elif col == "is_draft":
                value = int(bool(value))
            values.append(value)
        rows.append((repo_id, *values))
    conn.executemany(sql, rows)


def upsert_items(repo_id: int, items: list[dict[str, Any]]) -> None:
    """Insert or update items keyed by ``(repo_id, number)`` in one transaction."""
    with get_connection() as conn:
        _upsert_items(conn, repo_id, items)


def _sqlite_to_github_time(value: str | None) -> str | None:
    """'2026-10-02 12:00:00' (SQLite, UTC) -> '2026-10-02T12:00:00Z' (GitHub)."""
    return value.replace(" ", "T") + "Z" if value else None


def _known_current_through(conn: sqlite3.Connection, repo_id: int) -> str | None:
    """The time up to which every stored item of the repository is known to be current.

    ``repos.synced_through`` when recorded; for repositories imported before it
    existed, the oldest time any of its items was written (``imported_at``).
    """
    row = conn.execute("SELECT synced_through FROM repos WHERE id = ?", (repo_id,)).fetchone()
    if row and row["synced_through"]:
        return row["synced_through"]
    oldest = conn.execute(
        "SELECT MIN(imported_at) FROM items WHERE repo_id = ?", (repo_id,)
    ).fetchone()[0]
    return _sqlite_to_github_time(oldest)


def save_import(
    *, full_name: str, html_url: str, description: str, items: list[dict[str, Any]], started_at: str
) -> dict:
    """Store a whole import in **one** transaction: repository, items, PR merge dates.

    ``started_at`` (GitHub time when the import began) can only move the
    repository's ``synced_through`` back, never forward: an import refreshes the
    items it fetched, not the ones it skipped (e.g. closed items with
    ``state=open``), so those are still only known as of the earlier time.
    """
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
        previous = _known_current_through(conn, existing["id"]) if existing else None
        repo_id = _upsert_repo(conn, full_name, html_url, description)
        _upsert_items(conn, repo_id, items)
        _upsert_pr_basics(conn, repo_id, [i for i in items if i["kind"] == "pr"])
        through = min(previous, started_at) if previous else started_at
        conn.execute("UPDATE repos SET synced_through = ? WHERE id = ?", (through, repo_id))
    return get_repo(repo_id)  # type: ignore[return-value]


def sync_window_start(repo_id: int) -> str | None:
    with get_connection() as conn:
        return _known_current_through(conn, repo_id)


def save_sync(repo_id: int, items: list[dict[str, Any]], *, synced_through: str) -> dict:
    """Apply a synchronisation in **one** transaction and return what changed."""
    with get_connection() as conn:
        before = {
            row["number"]: row["state"]
            for row in conn.execute("SELECT number, state FROM items WHERE repo_id = ?", (repo_id,))
        }
        stats = {"added": 0, "updated": 0, "closed": 0, "reopened": 0}
        for item in items:
            old = before.get(item["number"])
            if old is None:
                stats["added"] += 1
                continue
            stats["updated"] += 1
            if old == "open" and item["state"] == "closed":
                stats["closed"] += 1
            elif old == "closed" and item["state"] == "open":
                stats["reopened"] += 1
        _upsert_items(conn, repo_id, items)
        _upsert_pr_basics(conn, repo_id, [i for i in items if i["kind"] == "pr"])
        conn.execute(
            "UPDATE repos SET synced_through = ?, last_synced_at = datetime('now') WHERE id = ?",
            (synced_through, repo_id),
        )
    return stats


def _item_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for col in _JSON_COLUMNS:
        data[col] = json.loads(data[col] or "[]")
    data["is_draft"] = bool(data["is_draft"])
    return data


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Suggestions a maintainer has not rejected (pending or confirmed).
_DUPLICATE_COUNT = """(SELECT COUNT(*) FROM duplicate_suggestions s
    WHERE (s.item_a = i.id OR s.item_b = i.id) AND s.status != 'rejected')"""


def list_items(
    *,
    filters: dict[str, str | None] | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    """Return ``(total_matching, page_of_items)``.

    Ordered by priority (high first), then most recently updated.
    ``q`` searches title and body (case-insensitive); ``#123`` or ``123``
    also matches the item number.
    """
    where: list[str] = []
    params: list[Any] = []
    for name, value in (filters or {}).items():
        if value is None or value == "":
            continue
        if name not in _ITEM_FILTERS:
            raise ValueError(f"Unknown filter: {name}")
        where.append(f"{_ITEM_FILTERS[name]} = ?")
        params.append(value)

    if q and q.strip():
        term = q.strip()
        like = f"%{_escape_like(term)}%"
        clause = "(i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\'"
        params.extend([like, like])
        number = term.lstrip("#")
        if number.isdigit():
            clause += " OR i.number = ?"
            params.append(int(number))
        where.append(clause + ")")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    base = f"FROM items i JOIN repos r ON r.id = i.repo_id {where_sql}"
    with get_connection() as conn:
        total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT i.*, r.full_name AS repo, {_DUPLICATE_COUNT} AS duplicate_count {base}
            ORDER BY CASE i.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     i.updated_at DESC, i.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return total, [_item_row_to_dict(row) for row in rows]


# --- Duplicate suggestions ----------------------------------------------------------

_DUP_STATUSES = ("pending", "confirmed", "rejected")


def get_repo_by_name(full_name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM repos WHERE full_name = ?", (full_name,)).fetchone()
    return get_repo(row["id"]) if row else None


def get_item(item_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            f"""
            SELECT i.*, r.full_name AS repo, {_DUPLICATE_COUNT} AS duplicate_count
            FROM items i JOIN repos r ON r.id = i.repo_id WHERE i.id = ?
            """,
            (item_id,),
        ).fetchone()
    return _item_row_to_dict(row) if row else None


def issues_for_duplicates(repo_id: int) -> list[dict]:
    """Issues (not PRs) of a repository with the fields duplicate detection needs."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, number, title, body, updated_at FROM items
            WHERE repo_id = ? AND kind = 'issue' ORDER BY id
            """,
            (repo_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_duplicates_state(repo_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT duplicates_fingerprint FROM repos WHERE id = ?", (repo_id,)
        ).fetchone()
    return row["duplicates_fingerprint"] if row else None


def save_duplicate_analysis(repo_id: int, pairs: list[dict], fingerprint: str) -> dict:
    """Replace the repository's *pending* suggestions with ``pairs`` in one transaction.

    Reviewed pairs (confirmed / rejected) are never deleted and their status is
    never changed; their similarity details are refreshed if the pair is found
    again. Returns counts by status.
    """
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM duplicate_suggestions WHERE repo_id = ? AND status = 'pending'", (repo_id,)
        )
        conn.executemany(
            """
            INSERT INTO duplicate_suggestions
                (repo_id, item_a, item_b, score, title_similarity, body_similarity, shared_terms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (item_a, item_b) DO UPDATE SET
                score = excluded.score,
                title_similarity = excluded.title_similarity,
                body_similarity = excluded.body_similarity,
                shared_terms = excluded.shared_terms,
                updated_at = datetime('now')
            """,
            [
                (
                    repo_id,
                    p["item_a"],
                    p["item_b"],
                    p["score"],
                    p["title_similarity"],
                    p["body_similarity"],
                    json.dumps(p["shared_terms"]),
                )
                for p in pairs
            ],
        )
        conn.execute(
            """
            UPDATE repos SET duplicates_fingerprint = ?, duplicates_analyzed_at = datetime('now')
            WHERE id = ?
            """,
            (fingerprint, repo_id),
        )
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM duplicate_suggestions WHERE repo_id = ? GROUP BY status",
                (repo_id,),
            ).fetchall()
        )
    return {status: counts.get(status, 0) for status in _DUP_STATUSES}


def _suggestion_row(row: sqlite3.Row, item_id: int) -> dict:
    data = dict(row)
    data["shared_terms"] = json.loads(data["shared_terms"] or "[]")
    other = "b" if data["item_a"] == item_id else "a"
    data["other"] = {
        "id": data.pop(f"{other}_id"),
        "number": data.pop(f"{other}_number"),
        "title": data.pop(f"{other}_title"),
        "html_url": data.pop(f"{other}_url"),
        "state": data.pop(f"{other}_state"),
    }
    for side in ("a", "b"):
        for key in ("id", "number", "title", "url", "state"):
            data.pop(f"{side}_{key}", None)
    return data


_SUGGESTION_SELECT = """
    SELECT s.*,
           a.id AS a_id, a.number AS a_number, a.title AS a_title, a.html_url AS a_url,
           a.state AS a_state,
           b.id AS b_id, b.number AS b_number, b.title AS b_title, b.html_url AS b_url,
           b.state AS b_state
    FROM duplicate_suggestions s
    JOIN items a ON a.id = s.item_a
    JOIN items b ON b.id = s.item_b
"""


def duplicates_for_item(item_id: int) -> list[dict]:
    """Suggestions involving ``item_id``: confirmed first, then pending, then rejected."""
    with get_connection() as conn:
        rows = conn.execute(
            _SUGGESTION_SELECT
            + """
            WHERE s.item_a = ? OR s.item_b = ?
            ORDER BY CASE s.status WHEN 'confirmed' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                     s.score DESC, s.id
            """,
            (item_id, item_id),
        ).fetchall()
    return [_suggestion_row(row, item_id) for row in rows]


def get_suggestion(suggestion_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM duplicate_suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["shared_terms"] = json.loads(data["shared_terms"] or "[]")
    return data


def set_suggestion_status(suggestion_id: int, status: str) -> dict | None:
    if status not in _DUP_STATUSES:
        raise ValueError(f"invalid status: {status}")
    with get_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE duplicate_suggestions
            SET status = ?, updated_at = datetime('now'),
                decided_at = CASE WHEN ? = 'pending' THEN NULL ELSE datetime('now') END
            WHERE id = ?
            """,
            (status, status, suggestion_id),
        )
        if cursor.rowcount == 0:
            return None
    return get_suggestion(suggestion_id)


def duplicate_counts(repo_id: int) -> dict:
    """Current suggestion counts by status for a repository."""
    with get_connection() as conn:
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM duplicate_suggestions WHERE repo_id = ? GROUP BY status",
                (repo_id,),
            ).fetchall()
        )
    return {status: counts.get(status, 0) for status in _DUP_STATUSES}


# --- Pull-request metadata ----------------------------------------------------------

_PR_SELECT = """
    SELECT i.id, i.number, i.title, i.state, i.author, i.html_url, i.is_draft,
           i.created_at, i.updated_at, i.closed_at, i.comments, i.github_labels,
           r.full_name AS repo, p.*
    FROM items i
    JOIN repos r ON r.id = i.repo_id
    LEFT JOIN pull_requests p ON p.item_id = i.id
"""


def _pr_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["id"] = data.get("id") or data.get("item_id")
    data["github_labels"] = json.loads(data.get("github_labels") or "[]")
    data["is_draft"] = bool(data["is_draft"])
    # No pull_requests row means the PR was imported before PR data was tracked,
    # so "not merged" cannot be told apart from "unknown".
    data["merge_known"] = data.get("item_id") is not None
    return data


def _upsert_pr_basics(conn: sqlite3.Connection, repo_id: int, prs: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO pull_requests (item_id, merged_at)
        SELECT id, ? FROM items WHERE repo_id = ? AND number = ? AND kind = 'pr'
        ON CONFLICT (item_id) DO UPDATE SET merged_at = excluded.merged_at
        """,
        [(pr.get("merged_at"), repo_id, pr["number"]) for pr in prs],
    )


def upsert_pr_basics(repo_id: int, prs: list[dict]) -> None:
    """Record data the import already has for PRs (merged date) without touching details."""
    with get_connection() as conn:
        _upsert_pr_basics(conn, repo_id, prs)


def save_pr_list(repo_id: int, pulls: list[dict]) -> int:
    """Store pulls-list metadata (branches, pending review requests). Returns rows matched."""
    rows = [
        (
            (p.get("head") or {}).get("ref"),
            (p.get("base") or {}).get("ref"),
            p.get("author_association"),
            len(p.get("requested_reviewers") or []),
            len(p.get("requested_teams") or []),
            p.get("merged_at"),
            repo_id,
            p["number"],
        )
        for p in pulls
        if p.get("number") is not None
    ]
    with get_connection() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO pull_requests (item_id, head_ref, base_ref, author_association,
                                       requested_reviewers, requested_teams, merged_at,
                                       list_synced_at)
            SELECT id, ?, ?, ?, ?, ?, ?, datetime('now') FROM items
            WHERE repo_id = ? AND number = ? AND kind = 'pr'
            ON CONFLICT (item_id) DO UPDATE SET
                head_ref = excluded.head_ref, base_ref = excluded.base_ref,
                author_association = excluded.author_association,
                requested_reviewers = excluded.requested_reviewers,
                requested_teams = excluded.requested_teams,
                merged_at = COALESCE(excluded.merged_at, pull_requests.merged_at),
                list_synced_at = excluded.list_synced_at
            """,
            rows,
        )
        return conn.total_changes - before


def save_pr_details(item_id: int, details: dict) -> None:
    """Store size and review data fetched for one PR."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO pull_requests (item_id, additions, deletions, changed_files, commits,
                review_comments, reviews_total, reviews_approved, reviews_changes_requested,
                reviews_commented, details_fetched_at, details_for_updated_at)
            SELECT id, :additions, :deletions, :changed_files, :commits, :review_comments,
                   :reviews_total, :reviews_approved, :reviews_changes_requested,
                   :reviews_commented, datetime('now'), updated_at
            FROM items WHERE id = :item_id AND kind = 'pr'
            ON CONFLICT (item_id) DO UPDATE SET
                additions = excluded.additions, deletions = excluded.deletions,
                changed_files = excluded.changed_files, commits = excluded.commits,
                review_comments = excluded.review_comments,
                reviews_total = excluded.reviews_total,
                reviews_approved = excluded.reviews_approved,
                reviews_changes_requested = excluded.reviews_changes_requested,
                reviews_commented = excluded.reviews_commented,
                details_fetched_at = excluded.details_fetched_at,
                details_for_updated_at = excluded.details_for_updated_at
            """,
            {
                "item_id": item_id,
                **{
                    k: details.get(k)
                    for k in (
                        "additions",
                        "deletions",
                        "changed_files",
                        "commits",
                        "review_comments",
                        "reviews_total",
                        "reviews_approved",
                        "reviews_changes_requested",
                        "reviews_commented",
                    )
                },
            },
        )


def list_prs(
    *, repo: str | None = None, author: str | None = None, q: str | None = None
) -> list[dict]:
    """All PR items matching the SQL-level filters, most recently updated first."""
    where, params = ["i.kind = 'pr'"], []
    if repo:
        where.append("r.full_name = ?")
        params.append(repo)
    if author:
        where.append("i.author = ? COLLATE NOCASE")
        params.append(author)
    if q and q.strip():
        term = q.strip()
        clause = "(i.title LIKE ? ESCAPE '\\' OR i.body LIKE ? ESCAPE '\\'"
        like = f"%{_escape_like(term)}%"
        params += [like, like]
        if term.lstrip("#").isdigit():
            clause += " OR i.number = ?"
            params.append(int(term.lstrip("#")))
        where.append(clause + ")")
    with get_connection() as conn:
        rows = conn.execute(
            _PR_SELECT + f" WHERE {' AND '.join(where)} ORDER BY i.updated_at DESC, i.id DESC",
            params,
        ).fetchall()
    return [_pr_row(row) for row in rows]


def get_pr(item_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(_PR_SELECT + " WHERE i.id = ? AND i.kind = 'pr'", (item_id,)).fetchone()
    return _pr_row(row) if row else None


def prs_needing_details(
    repo_id: int, *, limit: int, only_open: bool = True, force: bool = False
) -> tuple[list[dict], int]:
    """PRs whose size/review data is missing or older than the PR's last update.

    Returns ``(to_fetch, up_to_date_count)``.
    """
    state = "AND i.state = 'open'" if only_open else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT i.id, i.number, i.updated_at, p.details_fetched_at, p.details_for_updated_at
            FROM items i LEFT JOIN pull_requests p ON p.item_id = i.id
            WHERE i.repo_id = ? AND i.kind = 'pr' {state}
            ORDER BY i.updated_at DESC, i.id DESC
            """,
            (repo_id,),
        ).fetchall()
    stale = [
        dict(r)
        for r in rows
        if force
        or r["details_fetched_at"] is None
        or r["details_for_updated_at"] != r["updated_at"]
    ]
    return stale[:limit], len(rows) - len(stale)


def count_open_prs(repo_id: int) -> int:
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM items WHERE repo_id = ? AND kind = 'pr' AND state = 'open'",
            (repo_id,),
        ).fetchone()[0]
