"""The schema migration runs at startup.

The tracer bullet's other acceptance criterion: both `todos` and
`user_todo_views` exist after `make_db()`. Future tickets build rows in these
tables, so the contract is "after `make_db()`, the tables exist."
"""

from __future__ import annotations

import sqlite3


EXPECTED_TABLES = {"todos", "user_todo_views"}


def test_make_db_creates_expected_tables(db: sqlite3.Connection) -> None:
    rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    actual = {row["name"] for row in rows}

    assert EXPECTED_TABLES.issubset(actual), (
        f"make_db() must create {EXPECTED_TABLES}; got {actual}"
    )


def test_todos_table_has_expected_columns(db: sqlite3.Connection) -> None:
    rows = db.execute("PRAGMA table_info(todos)").fetchall()
    column_names = {row["name"] for row in rows}

    expected = {
        "id", "title", "description", "state",
        "created_by", "created_at", "updated_at",
        "completed_at", "deleted_at", "blocked_reason",
    }
    assert expected.issubset(column_names)


def test_user_todo_views_table_has_expected_columns(db: sqlite3.Connection) -> None:
    rows = db.execute("PRAGMA table_info(user_todo_views)").fetchall()
    column_names = {row["name"] for row in rows}

    expected = {"user_id", "todo_id", "position", "subscribed_at"}
    assert expected.issubset(column_names)


def test_make_db_is_fresh_per_connection() -> None:
    """Each `make_db()` call returns a brand-new in-memory database."""
    from todos.db import make_db

    a = make_db()
    b = make_db()

    a.execute(
        "INSERT INTO todos (title, state, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ephemeral", "pending", "alice", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    a.commit()

    rows_in_b = b.execute("SELECT COUNT(*) AS n FROM todos").fetchone()
    assert rows_in_b["n"] == 0
    a.close()
    b.close()