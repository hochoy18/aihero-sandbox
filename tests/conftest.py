"""Shared pytest fixtures for the tracer-bullet HTTP seam.

These fixtures are part of the contract for every subsequent ticket. They must
remain stable across tickets — the rule from issue #2 is that later work stands
on these fixtures without modifying them.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from todos.app import make_client
from todos.auth import DEFAULT_JWT_SECRET
from todos.db import make_db

# Pin the test secret before any production code path can read the env var.
# `_secret()` is evaluated at call time, not import time, but setting it here
# keeps the test environment unambiguous from the first line of the test
# session onward.
os.environ.setdefault("TODO_JWT_SECRET", DEFAULT_JWT_SECRET)


@pytest.fixture
def db():
    """A fresh in-memory SQLite database with the schema applied.

    Each test gets its own connection; the connection is closed at teardown so
    subsequent tests start from a clean slate.
    """
    conn = make_db()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def client(db) -> TestClient:
    """A TestClient wired to the test's `db` connection."""
    return make_client(db)


__all__ = ["make_db", "make_client", "DEFAULT_JWT_SECRET"]