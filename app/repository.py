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
    "number", "kind", "title", "body", "state", "author", "html_url", "github_labels",
    "comments", "reactions", "is_draft", "created_at", "updated_at", "closed_at",
    "category", "category_confidence", "priority", "owner_hint",
    "category_reasons", "priority_reasons", "category_source", "category_score",
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


def upsert_repo(full_name: str, html_url: str, description: str) -> dict:
    with get_connection() as conn:
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
        row = conn.execute(
            "SELECT id FROM repos WHERE full_name = ?", (full_name,)
        ).fetchone()
    return get_repo(row["id"])  # type: ignore[return-value]


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


def upsert_items(repo_id: int, items: list[dict[str, Any]]) -> None:
    """Insert or update items keyed by ``(repo_id, number)`` in one transaction."""
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
    with get_connection() as conn:
        conn.executemany(sql, rows)


def _item_row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for col in _JSON_COLUMNS:
        data[col] = json.loads(data[col] or "[]")
    data["is_draft"] = bool(data["is_draft"])
    return data


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
            SELECT i.*, r.full_name AS repo {base}
            ORDER BY CASE i.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     i.updated_at DESC, i.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return total, [_item_row_to_dict(row) for row in rows]
