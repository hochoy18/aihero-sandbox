"""Shared test helpers across the HTTP-seam test files.

Each ticket's test file started with its own `_create` / `_auth` pair
because the seam felt small enough that a copy-paste was cheaper than
a shared import. By issue #7 three files had grown near-identical
copies, so they now live here once and the test files import them.

`test_post_todos.py` keeps its own `_create` because the body shape
differs (it sends arbitrary JSON, not just a title).
"""

from __future__ import annotations

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
