"""Shared test helpers across the HTTP-seam test files.

Each ticket's test file started with its own `_create` / `_auth` pair
because the seam felt small enough that a copy-paste was cheaper than
a shared import. By issue #7 three files had grown near-identical
copies, so they now live here once and the test files import them.

The same threshold was crossed for `_insert_subscription` by issue #8
(three test files pinned a `user_todo_views` row directly), so it
joins the helpers here.

`test_post_todos.py` keeps its own `_create` because the body shape
differs (it sends arbitrary JSON, not just a title).
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from todos.auth import make_jwt


def _create(client: TestClient, sub: str, title: str = "x") -> int:
    """Mint a JWT for `sub`, POST a Todo, return its id.

    Asserts `201`; a non-201 response is a bug in the test, not a
    condition to handle gracefully here.
    """
    token = make_jwt(sub=sub)
    response = client.post(
        "/todos",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _auth(sub: str) -> dict[str, str]:
    """Return a fresh `Authorization: Bearer ...` header for `sub`."""
    return {"Authorization": f"Bearer {make_jwt(sub=sub)}"}


def _insert_subscription(
    db: sqlite3.Connection,
    *,
    user_id: str,
    todo_id: int,
    position: int = 1,
) -> None:
    """Insert a `user_todo_views` row directly.

    Lets a test pin the per-user `position` independently of the
    subscribe API's `max + 1` rule, so position-ordering ACs don't
    depend on the subscribe flow. The subscribe endpoint already
    exercises that ordering; here we want a deterministic ordering
    for the list.
    """
    db.execute(
        "INSERT INTO user_todo_views "
        "(user_id, todo_id, position, subscribed_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, todo_id, position, "2026-09-24T00:00:00Z"),
    )
    db.commit()

