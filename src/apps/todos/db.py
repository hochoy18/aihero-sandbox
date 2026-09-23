"""SQLite connection management and schema setup.

The tracer bullet's contract: `make_db()` returns a fresh, ready-to-use
in-memory SQLite connection with the `todos` and `user_todo_views` tables
present. Each call is independent — closing one connection does not affect
another, because `:memory:` databases live inside their connection.

Future tickets will add repository classes here (`TodoRepository`,
`UserTodoViewRepository`) that take a connection and run SQL against these
tables. Until then, the schema is the only thing the persistence layer
exposes.
"""

from __future__ import annotations

import sqlite3


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS todos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT    NOT NULL,
        description     TEXT    NOT NULL DEFAULT '',
        state           TEXT    NOT NULL,
        created_by      TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        updated_at      TEXT    NOT NULL,
        completed_at    TEXT,
        deleted_at      TEXT,
        blocked_reason  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_todo_views (
        user_id        TEXT    NOT NULL,
        todo_id        INTEGER NOT NULL,
        position       INTEGER NOT NULL,
        subscribed_at  TEXT    NOT NULL,
        PRIMARY KEY (user_id, todo_id)
    )
    """,
)


def make_db() -> sqlite3.Connection:
    """Create a fresh in-memory SQLite connection with the schema applied.

    The returned connection is configured with row-dict access (`row_factory =
    sqlite3.Row`) so callers can write `row["title"]` rather than indexing by
    column number. Foreign keys are enabled for future cross-table constraints.

    `check_same_thread=False` lets FastAPI's thread-pool route handlers reuse
    the connection created in the lifespan startup. Cross-thread write safety
    becomes a concern once real write traffic lands; that ticket will switch
    to a connection pool or add a `threading.Lock`.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()
    return conn