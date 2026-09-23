"""POST /todos creates a Todo with the caller's JWT `sub` as `created_by`.

Issue #3 acceptance criteria, expressed as behavior at the HTTP seam:

  * `POST /todos {"title": "..."}` with a valid JWT returns `201` and a body
    whose `created_by` matches the JWT `sub` and whose `state` is `pending`.
  * A row exists in the SQLite `todos` table after creation.
  * Missing or invalid JWT -> `401`.
  * Empty or missing `title` -> `422`.
  * Default response values: `description = null`, `state = pending`,
    `blocked_reason = null`.
  * The creator can immediately read it back via `GET /todos/{id}` even
    though no `user_todo_views` row was created.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from todos.auth import make_jwt


def _create(client: TestClient, sub: str, body: dict[str, Any]) -> Any:
    token = make_jwt(sub=sub)
    return client.post(
        "/todos", json=body, headers={"Authorization": f"Bearer {token}"}
    )


# --- happy path -------------------------------------------------------------


def test_post_todos_with_title_creates_with_creator_identity(
    client: TestClient,
) -> None:
    response = _create(client, "alice", {"title": "buy oat milk"})

    assert response.status_code == 201
    body = response.json()

    assert body["created_by"] == "alice"
    assert body["title"] == "buy oat milk"
    assert body["state"] == "pending"
    assert isinstance(body["id"], int)
    assert body["id"] > 0


def test_post_todos_persists_row_in_sqlite(
    client: TestClient, db: sqlite3.Connection
) -> None:
    response = _create(client, "alice", {"title": "buy oat milk"})

    assert response.status_code == 201
    todo_id = response.json()["id"]

    row = db.execute(
        "SELECT title, state, created_by FROM todos WHERE id = ?", (todo_id,)
    ).fetchone()

    assert row is not None
    assert row["title"] == "buy oat milk"
    assert row["state"] == "pending"
    assert row["created_by"] == "alice"


def test_post_todos_returns_default_values(
    client: TestClient,
) -> None:
    """No `description` provided -> response shows `description = null`.

    The DB column is `NOT NULL DEFAULT ''`; the API layer translates
    empty-string storage back to a `null` JSON value so consumers see the
    natural shape (`null` = "no description").
    """
    response = _create(client, "alice", {"title": "buy oat milk"})

    body = response.json()
    assert body["description"] is None
    assert body["state"] == "pending"
    assert body["blocked_reason"] is None
    # These fields have no value at creation time.
    assert body["completed_at"] is None
    assert body["deleted_at"] is None


def test_post_todos_accepts_optional_description(
    client: TestClient,
) -> None:
    response = _create(client, "alice", {"title": "x", "description": "extra"})

    assert response.status_code == 201
    assert response.json()["description"] == "extra"


def test_post_todos_returns_server_stamped_timestamps(
    client: TestClient,
) -> None:
    response = _create(client, "alice", {"title": "x"})

    body = response.json()
    assert body["created_at"] == body["updated_at"]
    # ISO 8601 UTC with a trailing Z, matching the tracer bullet's hard-coded
    # rows. Client cannot set these.
    assert body["created_at"].endswith("Z")


# --- creator can read it back without a subscription row ---------------------


def test_creator_can_get_own_todo_without_subscription(
    client: TestClient, db: sqlite3.Connection
) -> None:
    create_response = _create(client, "alice", {"title": "buy oat milk"})
    todo_id = create_response.json()["id"]

    # No subscription row exists.
    sub_row = db.execute(
        "SELECT 1 FROM user_todo_views WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    assert sub_row is None

    token = make_jwt(sub="alice")
    response = client.get(
        f"/todos/{todo_id}", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json()["id"] == todo_id
    assert response.json()["created_by"] == "alice"


# --- auth -------------------------------------------------------------------


def test_post_todos_without_auth_returns_401(client: TestClient) -> None:
    response = client.post("/todos", json={"title": "x"})
    assert response.status_code == 401


def test_post_todos_with_invalid_jwt_returns_401(client: TestClient) -> None:
    response = client.post(
        "/todos",
        json={"title": "x"},
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert response.status_code == 401


# --- body validation ---------------------------------------------------------


def test_post_todos_with_empty_title_returns_422(client: TestClient) -> None:
    response = _create(client, "alice", {"title": ""})
    assert response.status_code == 422


def test_post_todos_without_title_returns_422(client: TestClient) -> None:
    response = _create(client, "alice", {})
    assert response.status_code == 422


def test_post_todos_with_non_string_title_returns_422(
    client: TestClient,
) -> None:
    response = _create(client, "alice", {"title": 42})
    assert response.status_code == 422