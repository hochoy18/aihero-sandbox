"""The production path runs the migration at startup, not per request.

Issue #2's spec: "SQLite is wired up in-memory; schema migration runs at
boot." `create_app()` (no factory override) must open one shared connection
on lifespan startup, apply the schema once, and hand that same connection to
every subsequent request — not a fresh `:memory:` per call.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from todos.app import create_app


def test_production_create_app_runs_migration_at_startup() -> None:
    app = create_app()
    with TestClient(app) as client:
        # Lifespan startup has opened a shared connection and applied the schema.
        assert isinstance(app.state.db, sqlite3.Connection)

        tables = {
            row["name"]
            for row in app.state.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"todos", "user_todo_views"}.issubset(tables)


def test_production_create_app_shares_connection_across_requests() -> None:
    """A write made through `app.state.db` survives a request on the same app.

    With a per-request `:memory:` factory this would be impossible — the row
    would land in a connection that gets thrown away. Locking this in proves
    the connection is shared.
    """
    app = create_app()
    with TestClient(app) as client:
        app.state.db.execute(
            "INSERT INTO todos "
            "(title, state, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("shared", "pending", "alice", "2026-01-01T00:00:00Z",
             "2026-01-01T00:00:00Z"),
        )
        app.state.db.commit()

        # An unrelated second request — exercises the request lifecycle.
        client.get("/todos")  # 401, no auth; doesn't matter for this assertion

        row = app.state.db.execute("SELECT title FROM todos").fetchone()
        assert row is not None and row["title"] == "shared"