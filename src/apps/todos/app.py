"""HTTP application factory and test-client wiring.

Issue #2 specifies two things the factory must do:

1. Run the schema migration **at startup**, not per-request — so the wired-up
   in-memory database is ready before the first request lands.
2. Expose a `make_client(db)` seam so tests can inject their own connection
   and stand on this fixture for every later ticket.

Production path: `create_app()` opens one in-memory SQLite connection during
the FastAPI lifespan and shares it across every request. Writes survive within
the process lifetime; persistence across restarts is left to a later ticket.

Test path: `create_app(db_factory=...)` short-circuits the lifespan. The
factory is called per request, so tests can share a single connection
(`make_client(db)`) across the whole test.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from .auth import AuthUser, verify_jwt
from .db import make_db

DBFactory = Callable[[], sqlite3.Connection]


def create_app(db_factory: DBFactory | None = None) -> FastAPI:
    """Build a FastAPI app. Pass `db_factory` to inject a test connection."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if db_factory is None:
            # Production path: one shared in-memory connection for the
            # lifetime of the process.
            app.state.db = make_db()
            try:
                yield
            finally:
                app.state.db.close()
        else:
            # Test path: the caller owns the connection; we don't open one.
            yield

    app = FastAPI(title="aihero-todos", lifespan=lifespan)

    def get_db(request: Request) -> sqlite3.Connection:
        if db_factory is not None:
            return db_factory()
        return request.app.state.db

    @app.get("/todos")
    def list_todos(user: AuthUser = Depends(verify_jwt)) -> dict[str, list[dict[str, object]]]:
        # Tracer bullet: return an empty list. The subscription model and
        # `get_db` wiring are filled in by the next ticket.
        _ = user
        return {"todos": []}

    return app


def make_client(db: sqlite3.Connection) -> TestClient:
    """Build a TestClient sharing `db` across every request."""
    return TestClient(create_app(db_factory=lambda: db))