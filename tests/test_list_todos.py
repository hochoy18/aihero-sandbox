"""GET /todos — state filtering, default-active behaviour, ?state override.

Issue #8 acceptance criteria, expressed at the HTTP seam:

  * `GET /todos` returns only the caller's subscribed todos.
  * Without `?state=...`, the response is filtered to the active set
    (`pending`, `in_progress`); done / cancelled / deleted are excluded.
  * `?state=done` (or any single state) returns todos in that state.
  * `?state=pending,done,cancelled` returns the union.
  * Matching todos are ordered by the caller's per-user `position ASC`.
  * `?state=garbage` yields `422`.
  * Unauthenticated requests yield `401`.

The three-todo fixture (pending / done / cancelled, positions 1 / 2 / 3)
is reused as the negative-and-positive control: AC #2 expects the
default-active filter to surface only the `pending` todo, AC #4 expects
the explicit three-state filter to surface all three. Tests build on
that fixture to also cover `in_progress` and the `deleted`-not-default
AC.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from todos.auth import make_jwt

from ._helpers import _auth, _create, _insert_subscription


# --- helpers ---------------------------------------------------------------


def _set_state(
    client: TestClient, *, todo_id: int, sub: str, state: str
) -> dict[str, Any]:
    """PATCH a todo to the requested state and return the response body.

    `state` here is one of the legal TodoState values: `pending`,
    `in_progress`, `done`, `cancelled`, `deleted`. The test uses this
    helper to set up the three-todo fixture (pending / done / cancelled)
    plus a fourth `deleted` todo, rather than leaning on the state
    machine's literal current-state ordering.
    """
    current = client.get(f"/todos/{todo_id}", headers=_auth(sub))
    assert current.status_code == 200, (
        f"setup: failed to read todo {todo_id} (status {current.status_code})"
    )
    if_match = current.json()["updated_at"]
    response = client.patch(
        f"/todos/{todo_id}",
        json={"state": state},
        headers={**_auth(sub), "If-Match": if_match},
    )
    assert response.status_code == 200, (
        f"setup: failed to set state {state} on todo {todo_id} "
        f"(status {response.status_code}: {response.text})"
    )
    return response.json()


def _list(
    client: TestClient,
    *,
    sub: str,
    query: str = "",
) -> Any:
    """GET /todos with the given query string."""
    return client.get(f"/todos{query}", headers=_auth(sub))


# --- fixture: pending / done / cancelled, positions 1 / 2 / 3 --------------


def _build_three_state_fixture(
    client: TestClient, db: sqlite3.Connection
) -> tuple[int, int, int]:
    """Build the three-todo fixture from AC #1.

    Returns `(pending_id, done_id, cancelled_id)` in that order so the
    test bodies read as "the pending todo is id[0]" without re-reading
    titles.

    Note: only Alice (the creator) can transition her todos to
    `done`, `cancelled`, or `deleted`. Bob is a subscriber, not a
    state-mutator. State changes land on Alice's session; Bob's
    subscription just lets him read.
    """
    pending_id = _create(client, "alice", title="pending-1")
    done_id = _create(client, "alice", title="done-2")
    cancelled_id = _create(client, "alice", title="cancelled-3")

    # Pin Bob's positions to 1, 2, 3 to match AC #1.
    _insert_subscription(db, user_id="bob", todo_id=pending_id, position=1)
    _insert_subscription(db, user_id="bob", todo_id=done_id, position=2)
    _insert_subscription(db, user_id="bob", todo_id=cancelled_id, position=3)

    _set_state(client, todo_id=done_id, sub="alice", state="done")
    _set_state(client, todo_id=cancelled_id, sub="alice", state="cancelled")

    return pending_id, done_id, cancelled_id


# --- default-active behaviour ----------------------------------------------


def test_get_todos_default_returns_only_active(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: no `?state` -> only `pending` shows up; `done` and
    `cancelled` are filtered out by the default-active behaviour.

    This is the headline AC for issue #8: the three-todo fixture
    (positions 1 / 2 / 3, states pending / done / cancelled) collapses
    to a single `pending` row in the default response.
    """
    pending_id, _, _ = _build_three_state_fixture(client, db)

    response = _list(client, sub="bob")

    assert response.status_code == 200
    body = response.json()
    assert [t["id"] for t in body["todos"]] == [pending_id]


def test_get_todos_default_excludes_done_and_cancelled(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Negative control for the default-active behaviour.

    Independently of the single-todo assertion above, this test asserts
    that the `done` and `cancelled` todos are *not* in the response —
    so a regression that returns them as zero-row entries (e.g. an
    empty Todo with the wrong `id`) gets caught.
    """
    _, done_id, cancelled_id = _build_three_state_fixture(client, db)

    response = _list(client, sub="bob")

    assert response.status_code == 200
    ids = [t["id"] for t in response.json()["todos"]]
    assert done_id not in ids
    assert cancelled_id not in ids


def test_get_todos_default_includes_in_progress(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """`in_progress` is part of the active set; default surfaces it.

    The spec calls out `pending` and `in_progress` as the active pair;
    this test pins both ends so a regression that hardcodes `[pending]`
    alone is caught.
    """
    pending_id = _create(client, "alice", title="pending")
    in_progress_id = _create(client, "alice", title="in-progress")
    _set_state(client, todo_id=in_progress_id, sub="alice", state="in_progress")

    _insert_subscription(db, user_id="bob", todo_id=pending_id, position=1)
    _insert_subscription(db, user_id="bob", todo_id=in_progress_id, position=2)

    response = _list(client, sub="bob")

    assert response.status_code == 200
    assert [t["id"] for t in response.json()["todos"]] == [
        pending_id,
        in_progress_id,
    ]


# --- explicit ?state override ----------------------------------------------


def test_get_todos_with_single_state_returns_only_matching(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: `?state=done` -> only the `done` todo."""
    _, done_id, _ = _build_three_state_fixture(client, db)

    response = _list(client, sub="bob", query="?state=done")

    assert response.status_code == 200
    assert [t["id"] for t in response.json()["todos"]] == [done_id]


def test_get_todos_with_multiple_states_returns_union(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: `?state=pending,done,cancelled` -> all three, in position order.

    The AC wording uses `completed` as a placeholder for the terminal
    state; the canonical state name is `done`. We send the three
    states that the fixture actually contains so the test exercises
    the union path the AC describes.
    """
    pending_id, done_id, cancelled_id = _build_three_state_fixture(
        client, db
    )

    response = _list(
        client, sub="bob", query="?state=pending,done,cancelled"
    )

    assert response.status_code == 200
    assert [t["id"] for t in response.json()["todos"]] == [
        pending_id,
        done_id,
        cancelled_id,
    ]


def test_get_todos_with_states_preserves_position_order(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: when multiple todos match, ordering is `position ASC`.

    Subscribe out of position order to make sure the result is driven
    by `position`, not by subscribe order or by state name.
    """
    in_progress_id = _create(client, "alice", title="ip")
    pending_id = _create(client, "alice", title="p")
    done_id = _create(client, "alice", title="d")
    _set_state(client, todo_id=in_progress_id, sub="alice", state="in_progress")
    _set_state(client, todo_id=done_id, sub="alice", state="done")

    # Positions 3, 1, 2 — deliberately non-monotonic with creation order.
    _insert_subscription(db, user_id="bob", todo_id=in_progress_id, position=3)
    _insert_subscription(db, user_id="bob", todo_id=pending_id, position=1)
    _insert_subscription(db, user_id="bob", todo_id=done_id, position=2)

    response = _list(
        client, sub="bob", query="?state=pending,in_progress,done"
    )

    assert response.status_code == 200
    assert [t["id"] for t in response.json()["todos"]] == [
        pending_id,
        done_id,
        in_progress_id,
    ]


def test_get_todos_state_filter_tolerates_whitespace(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """`?state=pending, done` is equivalent to `?state=pending,done`.

    Whitespace around each comma-separated token is trimmed so a
    caller building the query from a list comprehension doesn't have
    to be careful about joining commas.
    """
    pending_id, done_id, _ = _build_three_state_fixture(client, db)

    response = _list(
        client, sub="bob", query="?state=pending,%20done"
    )

    assert response.status_code == 200
    assert [t["id"] for t in response.json()["todos"]] == [
        pending_id,
        done_id,
    ]


def test_get_todos_empty_state_param_uses_default(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """`?state=` (empty value) -> default-active behaviour.

    Some clients send an empty query parameter by accident; treating
    that as "no filter" matches the absent-parameter case so the
    response stays predictable.
    """
    pending_id, _, _ = _build_three_state_fixture(client, db)

    response = _list(client, sub="bob", query="?state=")

    assert response.status_code == 200
    assert [t["id"] for t in response.json()["todos"]] == [pending_id]


def test_get_todos_state_with_no_matches_returns_empty(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """A valid filter that excludes every todo returns `[]`, not `404`.

    This is the natural extension of the filter rule: the response is
    "no todos match", and an empty list is the right shape — a `404`
    would imply the user themselves is unknown, which isn't what's
    being asked.
    """
    _build_three_state_fixture(client, db)
    # Bob has subscriptions across pending / done / cancelled, but a
    # `deleted`-only filter has no matches. The response is empty.

    response = _list(client, sub="bob", query="?state=deleted")

    assert response.status_code == 200
    assert response.json()["todos"] == []


# --- deleted is not in the default active set -----------------------------


def test_get_todos_default_excludes_deleted(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: a subscription to a `deleted` todo does not surface via
    the default list, because `deleted` is not in the default active
    set.

    The creator (Alice) soft-deletes her own todo; Bob remains
    subscribed (a subscription row is not auto-removed on delete).
    Default `GET /todos` for Bob returns nothing, even though the row
    still exists in storage.
    """
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id, position=1)
    _set_state(client, todo_id=todo_id, sub="alice", state="deleted")

    response = _list(client, sub="bob")

    assert response.status_code == 200
    assert response.json()["todos"] == []


def test_get_todos_explicit_deleted_filter_surfaces_deleted_todo(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """`?state=deleted` is the only way to surface a deleted todo
    via the list endpoint.

    Pairs with `test_get_todos_default_excludes_deleted`: the deleted
    row is in storage and Bob's subscription row is still present, so
    the explicit filter is the only thing that can return it.
    """
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id, position=1)
    _set_state(client, todo_id=todo_id, sub="alice", state="deleted")

    response = _list(client, sub="bob", query="?state=deleted")

    assert response.status_code == 200
    assert [t["id"] for t in response.json()["todos"]] == [todo_id]


# --- invalid input ---------------------------------------------------------


def test_get_todos_with_unknown_state_returns_422(
    client: TestClient,
) -> None:
    """`?state=garbage` -> 422 with a useful detail body.

    An unknown state name is a malformed query, not a runtime error;
    the route layer owns the rejection because the domain enum would
    also reject it but with a less specific exception.
    """
    response = _list(client, sub="bob", query="?state=garbage")
    assert response.status_code == 422


def test_get_todos_with_mixed_valid_and_invalid_state_returns_422(
    client: TestClient,
) -> None:
    """A single invalid token poisons the whole filter.

    We don't partially apply the recognised names — a partial filter
    would silently misrepresent what the caller asked for, which is
    worse than a clear 422.
    """
    response = _list(client, sub="bob", query="?state=pending,garbage")
    assert response.status_code == 422


# --- auth -----------------------------------------------------------------


def test_get_todos_without_auth_returns_401(client: TestClient) -> None:
    """AC: no JWT -> 401.

    The auth check fires before any filter logic; this guards against
    a regression where the filter runs first and accidentally returns
    data on an unauthenticated request.
    """
    response = client.get("/todos")
    assert response.status_code == 401


def test_get_todos_with_invalid_jwt_returns_401(client: TestClient) -> None:
    """A bad bearer token still yields 401, regardless of `?state`."""
    response = client.get(
        "/todos?state=pending",
        headers={"Authorization": "Bearer not.a.real.jwt"},
    )
    assert response.status_code == 401


def test_get_todos_with_state_but_no_auth_returns_401(
    client: TestClient,
) -> None:
    """Auth precedence: a valid `?state` query parameter does not
    bypass the auth requirement.

    Re-asserts the auth check after the query-param parsing changes
    so the new code path doesn't accidentally drop the auth
    dependency.
    """
    response = client.get("/todos?state=pending,done")
    assert response.status_code == 401
