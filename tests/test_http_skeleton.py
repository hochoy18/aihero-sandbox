"""HTTP skeleton — the tracer bullet every later ticket stands on.

Issue #2 acceptance criteria, expressed as behavior:
  * GET /todos with a valid JWT returns 200 with `{"todos": []}`.
  * GET /todos without an Authorization header returns 401.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from todos.auth import make_jwt


def test_get_todos_without_auth_returns_401(client: TestClient) -> None:
    response = client.get("/todos")
    assert response.status_code == 401


def test_get_todos_with_valid_jwt_returns_empty_list(client: TestClient) -> None:
    token = make_jwt(sub="alice")
    response = client.get("/todos", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"todos": []}


def test_get_todos_with_malformed_authorization_returns_401(client: TestClient) -> None:
    response = client.get("/todos", headers={"Authorization": "NotBearer abc.def.ghi"})
    assert response.status_code == 401


def test_get_todos_with_invalid_jwt_returns_401(client: TestClient) -> None:
    response = client.get("/todos", headers={"Authorization": "Bearer not.a.real.jwt"})
    assert response.status_code == 401