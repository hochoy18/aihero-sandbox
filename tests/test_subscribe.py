"""POST /todos/{id}/subscribe and DELETE /todos/{id}/subscribe.

Issue #7 acceptance criteria, expressed at the HTTP seam:

  * `POST /todos/{id}/subscribe` returns `201`; a `user_todo_views` row
    is created with `position = max(B.position) + 1` (1 for the first
    subscription).
  * B's `GET /todos` includes the newly subscribed todo afterward.
  * `DELETE /todos/{id}/subscribe` returns `204`; the row is removed;
    `GET /todos` no longer shows it.
  * Subscribing twice to the same todo → `422`.
  * Subscribing to a non-existent todo → `404`.
  * Missing or invalid JWT → `401`.

The "default position" rule (`max(user.position) + 1`) is exercised
explicitly so a regression that always writes `1` is caught.

`GET /todos` is touched here as a verification mechanism for AC #2 and
#3 — its filtering / state-default behaviour lands in #8.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from todos.auth import make_jwt

from ._helpers import _auth, _create


# --- helpers ---------------------------------------------------------------


def _subscribe(client: TestClient, *, sub: str, todo_id: int) -> Any:
    return client.post(
        f"/todos/{todo_id}/subscribe",
        headers=_auth(sub),
    )


def _unsubscribe(client: TestClient, *, sub: str, todo_id: int) -> Any:
    return client.delete(
        f"/todos/{todo_id}/subscribe",
        headers=_auth(sub),
    )


def _view_row(
    db: sqlite3.Connection, *, user_id: str, todo_id: int
) -> sqlite3.Row | None:
    return db.execute(
        "SELECT user_id, todo_id, position, subscribed_at "
        "FROM user_todo_views WHERE user_id = ? AND todo_id = ?",
        (user_id, todo_id),
    ).fetchone()


# --- POST /todos/{id}/subscribe — happy path -------------------------------


def test_subscribe_first_for_user_returns_201(client: TestClient) -> None:
    """AC: B's first subscription → 201."""
    todo_id = _create(client, "alice")

    response = _subscribe(client, sub="bob", todo_id=todo_id)

    assert response.status_code == 201


def test_subscribe_creates_view_row_with_position_one(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: first subscription lands at position 1 (max(B.position)+1, max is None)."""
    todo_id = _create(client, "alice")

    _subscribe(client, sub="bob", todo_id=todo_id)

    row = _view_row(db, user_id="bob", todo_id=todo_id)
    assert row is not None
    assert row["position"] == 1
    assert row["subscribed_at"].endswith("Z")


def test_subscribe_second_for_user_uses_max_plus_one(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: subsequent subscription position = max(B.position) + 1.

    Bob subscribes to two different todos. The second position must be
    one greater than the first, not always 1 — a regression that pins
    position to 1 would silently break the per-user ordering.
    """
    first = _create(client, "alice", title="first")
    second = _create(client, "alice", title="second")

    _subscribe(client, sub="bob", todo_id=first)
    _subscribe(client, sub="bob", todo_id=second)

    first_row = _view_row(db, user_id="bob", todo_id=first)
    second_row = _view_row(db, user_id="bob", todo_id=second)
    assert first_row is not None
    assert second_row is not None
    assert first_row["position"] == 1
    assert second_row["position"] == 2


def test_subscribe_does_not_affect_other_users_positions(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Position is per-user, not global.

    Alice's high position must not bump Bob's first subscription above
    1; otherwise reordering and per-user position math would mix.
    """
    a_todo = _create(client, "alice", title="a")
    b_todo = _create(client, "alice", title="b")
    c_todo = _create(client, "alice", title="c")

    # Alice subscribes to a, b, c — her positions climb 1, 2, 3.
    _subscribe(client, sub="alice", todo_id=a_todo)
    _subscribe(client, sub="alice", todo_id=b_todo)
    _subscribe(client, sub="alice", todo_id=c_todo)
    assert _view_row(db, user_id="alice", todo_id=c_todo)["position"] == 3

    # Bob's first subscription is still position 1.
    _subscribe(client, sub="bob", todo_id=a_todo)
    assert _view_row(db, user_id="bob", todo_id=a_todo)["position"] == 1


def test_subscribe_after_unsubscribe_lands_at_one(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Position resets after unsubscribe (max of current rows, not history).

    Bob subscribes to a, then unsubscribes, then subscribes again. With
    no remaining rows the max is `NULL` (i.e. 0), so the new position is
    1 — not 2. This documents the rule: positions are dense on the
    current view, not monotonic across history.
    """
    todo_id = _create(client, "alice")

    _subscribe(client, sub="bob", todo_id=todo_id)
    assert _view_row(db, user_id="bob", todo_id=todo_id)["position"] == 1
    _unsubscribe(client, sub="bob", todo_id=todo_id)

    _subscribe(client, sub="bob", todo_id=todo_id)
    assert _view_row(db, user_id="bob", todo_id=todo_id)["position"] == 1


# --- POST /todos/{id}/subscribe — visibility in GET /todos -----------------


def test_get_todos_includes_subscribed_todo(
    client: TestClient,
) -> None:
    """AC: B's `GET /todos` includes the newly subscribed todo."""
    todo_id = _create(client, "alice")
    _subscribe(client, sub="bob", todo_id=todo_id)

    response = client.get("/todos", headers=_auth("bob"))

    assert response.status_code == 200
    body = response.json()
    assert [t["id"] for t in body["todos"]] == [todo_id]


def test_get_todos_excludes_todo_before_subscribe(
    client: TestClient,
) -> None:
    """Bob has not subscribed; the todo doesn't appear in his view.

    This is the negative control: the visible-after-subscribe AC only
    proves the True path of subscribe→visible; this proves the False
    path of not-subscribed→invisible.
    """
    todo_id = _create(client, "alice")

    response = client.get("/todos", headers=_auth("bob"))

    assert response.status_code == 200
    assert response.json()["todos"] == []


def test_get_todos_excludes_unsubscribed_todo(
    client: TestClient,
) -> None:
    """AC: after unsubscribe, `GET /todos` no longer shows the todo."""
    todo_id = _create(client, "alice")
    _subscribe(client, sub="bob", todo_id=todo_id)
    _unsubscribe(client, sub="bob", todo_id=todo_id)

    response = client.get("/todos", headers=_auth("bob"))

    assert response.status_code == 200
    assert response.json()["todos"] == []


def test_get_todos_returns_todos_in_position_order(
    client: TestClient,
) -> None:
    """GET /todos is ordered by the user's `position ASC`."""
    first = _create(client, "alice", title="first")
    second = _create(client, "alice", title="second")
    third = _create(client, "alice", title="third")

    # Subscribe in a non-monotonic order to make sure the listing is
    # driven by position, not by insert order.
    _subscribe(client, sub="bob", todo_id=third)
    _subscribe(client, sub="bob", todo_id=first)
    _subscribe(client, sub="bob", todo_id=second)

    response = client.get("/todos", headers=_auth("bob"))
    body = response.json()
    assert [t["id"] for t in body["todos"]] == [third, first, second]


def test_get_todos_for_creator_includes_self_created(
    client: TestClient,
) -> None:
    """The creator must subscribe explicitly to see their own todo.

    Creator-bypass only grants *read* access (GET /todos/{id}); the
    list view is built from `user_todo_views`, which the creator's
    subscription row populates. Without an explicit subscribe, the
    creator's own todo is invisible in their list.
    """
    todo_id = _create(client, "alice")

    response = client.get("/todos", headers=_auth("alice"))
    assert response.json()["todos"] == []

    _subscribe(client, sub="alice", todo_id=todo_id)

    response = client.get("/todos", headers=_auth("alice"))
    assert [t["id"] for t in response.json()["todos"]] == [todo_id]


# --- POST /todos/{id}/subscribe — error paths ------------------------------


def test_subscribe_to_nonexistent_todo_returns_404(
    client: TestClient,
) -> None:
    """AC: subscribing to a non-existent todo → 404."""
    response = _subscribe(client, sub="bob", todo_id=99999)
    assert response.status_code == 404


def test_subscribe_twice_to_same_todo_returns_422(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: double subscribe → 422.

    `user_todo_views` has `PRIMARY KEY (user_id, todo_id)`, so the SQL
    insert would raise `UNIQUE` constraint failure; we surface that as
    a domain-level `AlreadySubscribedError` (422) so the HTTP layer
    doesn't have to translate sqlite3 errors.

    A row exists after the first subscribe; the second subscribe must
    not mutate the row.
    """
    todo_id = _create(client, "alice")

    first = _subscribe(client, sub="bob", todo_id=todo_id)
    assert first.status_code == 201
    before = _view_row(db, user_id="bob", todo_id=todo_id)

    second = _subscribe(client, sub="bob", todo_id=todo_id)
    assert second.status_code == 422

    after = _view_row(db, user_id="bob", todo_id=todo_id)
    assert after is not None
    assert dict(after) == dict(before)


def test_subscribe_without_auth_returns_401(client: TestClient) -> None:
    """AC: missing JWT → 401."""
    todo_id = _create(client, "alice")
    response = client.post(f"/todos/{todo_id}/subscribe")
    assert response.status_code == 401


def test_subscribe_with_invalid_jwt_returns_401(
    client: TestClient,
) -> None:
    todo_id = _create(client, "alice")
    response = client.post(
        f"/todos/{todo_id}/subscribe",
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert response.status_code == 401


# --- DELETE /todos/{id}/subscribe -----------------------------------------


def test_unsubscribe_returns_204_and_removes_row(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: DELETE → 204; the row is removed."""
    todo_id = _create(client, "alice")
    _subscribe(client, sub="bob", todo_id=todo_id)
    assert _view_row(db, user_id="bob", todo_id=todo_id) is not None

    response = _unsubscribe(client, sub="bob", todo_id=todo_id)
    assert response.status_code == 204
    assert _view_row(db, user_id="bob", todo_id=todo_id) is None


def test_unsubscribe_when_not_subscribed_returns_204(
    client: TestClient,
) -> None:
    """DELETE is idempotent: unsubscribing from a todo you never joined
    is a 204, not a 404. The spec doesn't mandate idempotency but the
    REST convention is "the resource is in the desired state after
    this call", and forcing the client to check first is busywork.
    """
    todo_id = _create(client, "alice")
    response = _unsubscribe(client, sub="bob", todo_id=todo_id)
    assert response.status_code == 204


def test_unsubscribe_for_nonexistent_todo_returns_204(
    client: TestClient,
) -> None:
    """Same idempotency rule for a missing todo: 204. There's no row
    to delete, so we're already in the "no subscription" state.
    """
    response = _unsubscribe(client, sub="bob", todo_id=99999)
    assert response.status_code == 204


def test_unsubscribe_without_auth_returns_401(
    client: TestClient,
) -> None:
    todo_id = _create(client, "alice")
    response = client.delete(f"/todos/{todo_id}/subscribe")
    assert response.status_code == 401


def test_unsubscribe_with_invalid_jwt_returns_401(
    client: TestClient,
) -> None:
    todo_id = _create(client, "alice")
    response = client.delete(
        f"/todos/{todo_id}/subscribe",
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert response.status_code == 401


def test_unsubscribe_only_affects_caller(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Bob unsubscribing must not touch Alice's view row."""
    todo_id = _create(client, "alice")
    _subscribe(client, sub="alice", todo_id=todo_id)
    _subscribe(client, sub="bob", todo_id=todo_id)

    _unsubscribe(client, sub="bob", todo_id=todo_id)

    assert _view_row(db, user_id="bob", todo_id=todo_id) is None
    assert _view_row(db, user_id="alice", todo_id=todo_id) is not None
