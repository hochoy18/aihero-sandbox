"""GET /todos/{id} — creator bypass + subscription gate.

Issue #4 acceptance criteria, expressed at the HTTP seam:

  * Creator can read their own todo even with no `user_todo_views` row.
  * Any other authenticated user that has not subscribed gets `404`.
  * Non-existent id also returns `404`.
  * The two `404` cases are byte-for-byte indistinguishable (no existence leak).
  * Missing or invalid JWT still returns `401`.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from todos.auth import make_jwt

from ._helpers import _auth, _create


def _insert_subscription(
    db: sqlite3.Connection, *, user_id: str, todo_id: int, position: int = 1
) -> None:
    """Insert a `user_todo_views` row directly.

    `POST /todos/{id}/subscribe` lands in issue #9; until then we seed the
    subscription table from the test to exercise the read-gate's positive
    path.
    """
    db.execute(
        "INSERT INTO user_todo_views "
        "(user_id, todo_id, position, subscribed_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, todo_id, position, "2026-09-24T00:00:00Z"),
    )
    db.commit()


# --- happy path -------------------------------------------------------------


def test_get_todo_by_id_creator_returns_200(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Creator can read their own todo even with no `user_todo_views` row.

    Asserting the absence of the subscription row guards against a
    regression where creator reads silently depend on a subscription row
    being present (issue #4 AC #1).
    """
    todo_id = _create(client, "alice", title="buy oat milk")

    response = client.get(f"/todos/{todo_id}", headers=_auth("alice"))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == todo_id
    assert body["title"] == "buy oat milk"
    assert body["created_by"] == "alice"

    # No subscription row exists for the creator — the bypass is real.
    row = db.execute(
        "SELECT 1 FROM user_todo_views WHERE user_id = ? AND todo_id = ?",
        ("alice", todo_id),
    ).fetchone()
    assert row is None


def test_get_todo_by_id_subscribed_non_creator_returns_200(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """A non-creator with a subscription row can read the todo.

    Positive control for the subscription gate: AC #2 ("not subscribed
    returns 404") only proves the False path of `has_view`. This test
    proves the True path also reaches a 200, so the gate actually grants
    access (and not just denies it).
    """
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id)

    response = client.get(f"/todos/{todo_id}", headers=_auth("bob"))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == todo_id
    assert body["created_by"] == "alice"


# --- subscription gate ------------------------------------------------------


def test_get_todo_by_id_non_subscriber_returns_404(client: TestClient) -> None:
    """A user that didn't subscribe (and isn't the creator) gets 404.

    No `POST /todos/{id}/subscribe` route exists yet, so the only way to
    be "not subscribed" in this test is to never have called subscribe —
    which is the default state for every user.
    """
    todo_id = _create(client, "alice")

    response = client.get(f"/todos/{todo_id}", headers=_auth("bob"))

    assert response.status_code == 404


def test_get_todo_by_id_404_responses_are_indistinguishable(
    client: TestClient,
) -> None:
    """The 404 returned for a non-existent id must be byte-for-byte identical
    to the 404 returned for a todo that exists but the caller doesn't own and
    hasn't subscribed to. Otherwise we leak existence.
    """
    todo_id = _create(client, "alice")

    missing_response = client.get("/todos/99999", headers=_auth("alice"))
    forbidden_response = client.get(f"/todos/{todo_id}", headers=_auth("bob"))

    assert missing_response.status_code == 404
    assert forbidden_response.status_code == 404

    # Body: same bytes from the same JSON encoder.
    assert missing_response.content == forbidden_response.content
    assert missing_response.json() == forbidden_response.json()

    # Headers: same set and same values, modulo per-request `date`.
    missing_headers = {
        k: v for k, v in missing_response.headers.items() if k.lower() != "date"
    }
    forbidden_headers = {
        k: v for k, v in forbidden_response.headers.items() if k.lower() != "date"
    }
    assert missing_headers == forbidden_headers


def test_get_todo_by_id_nonexistent_returns_404(client: TestClient) -> None:
    response = client.get("/todos/99999", headers=_auth("alice"))
    assert response.status_code == 404


# --- auth -------------------------------------------------------------------


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