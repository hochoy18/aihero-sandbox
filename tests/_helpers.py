"""Shared test helpers across the HTTP-seam test files.

Each ticket's test file started with its own `_create` / `_auth` pair
because the seam felt small enough that a copy-paste was cheaper than
a shared import. By issue #7 three files had grown near-identical
copies, so they now live here once and the test files import them.

`_insert_subscription` lands here by issue #8: the two existing files
that pinned a `user_todo_views` row inline (`test_get_todo_by_id.py`,
`test_patch_todo.py`) and the new `test_list_todos.py` all need a
deterministic per-user `position`, and the subscribe endpoint's
`max + 1` rule is exactly what those tests want to bypass.

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

