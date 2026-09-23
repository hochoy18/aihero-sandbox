"""GET /todos/{id} — creator can always read their own; 404 for non-existent.

This is the minimal slice of issue #4 needed to satisfy issue #3's acceptance
criterion that the creator can read back the todo they just created. The full
subscription gate (`404` for non-subscribers, byte-identical response to
non-existent ids) lands in #4.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from todos.auth import make_jwt


def _create(client: TestClient, sub: str, title: str = "x") -> int:
    token = make_jwt(sub=sub)
    response = client.post(
        "/todos",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_get_todo_by_id_creator_returns_200(client: TestClient) -> None:
    todo_id = _create(client, "alice", title="buy oat milk")

    token = make_jwt(sub="alice")
    response = client.get(
        f"/todos/{todo_id}", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == todo_id
    assert body["title"] == "buy oat milk"
    assert body["created_by"] == "alice"


def test_get_todo_by_id_nonexistent_returns_404(client: TestClient) -> None:
    token = make_jwt(sub="alice")
    response = client.get(
        "/todos/99999", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404


def test_get_todo_by_id_without_auth_returns_401(client: TestClient) -> None:
    todo_id = _create(client, "alice")
    response = client.get(f"/todos/{todo_id}")
    assert response.status_code == 401


def test_get_todo_by_id_with_invalid_jwt_returns_401(
    client: TestClient,
) -> None:
    todo_id = _create(client, "alice")
    response = client.get(
        f"/todos/{todo_id}",
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert response.status_code == 401