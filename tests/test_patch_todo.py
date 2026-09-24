"""PATCH /todos/{id} — state machine, creator-only delete, blocked_reason, If-Match.

Issue #5 acceptance criteria, expressed at the HTTP seam:

  * Legal state transitions return 200 and refresh `updated_at`.
  * Illegal transitions (e.g. `done` -> `in_progress`) return 409.
  * Transitioning to `deleted` is creator-only: non-creator returns 409,
    creator returns 200 and `deleted_at` is stamped.
  * `blocked_reason` is freely writable by any user with access; setting
    it to `null` clears the value.
  * PATCH without `If-Match` returns 409.
  * The `If-Match` value is accepted regardless of correctness in this
    ticket (validation lands in T3b / #6); on success, the server
    refreshes `updated_at`.
  * Invalid body shape (e.g. `title: ""`) returns 422.
  * Missing or invalid JWT still returns 401.
  * A non-subscriber (non-creator) still gets 404 from the subscription
    gate; the gate hides the row before state-machine logic runs.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi.testclient import TestClient

from todos.auth import make_jwt

from ._helpers import _auth, _create, _insert_subscription


# --- helpers ---------------------------------------------------------------


def _patch(
    client: TestClient,
    *,
    todo_id: int,
    sub: str,
    body: dict[str, Any],
    if_match: str | None = "auto",
) -> Any:
    """Send a PATCH, with the If-Match value resolved for the caller.

    By default the helper fetches the row's current `updated_at` and
    sends it as the `If-Match` header, so most tests can stay focused on
    the behavior they actually exercise (state machine, blocked_reason,
    access checks) instead of threading timestamps through every call.
    Pass an explicit `if_match=<value>` to send a specific header (used
    by the T3b tests for stale / fresh / matching values); pass
    `if_match=None` to omit the header entirely (used by
    `test_patch_without_if_match_returns_409`).

    The auto-fetch silently no-ops when the row is not visible to `sub`
    (a `GET` returns `404` because the row doesn't exist or the caller
    isn't subscribed). In that case the PATCH carries no `If-Match`
    header, so the subscription gate fires first and returns the same
    `404` envelope — preserving the behavior the access-check tests
    assert against.
    """
    headers = _auth(sub)
    if if_match == "auto":
        current = client.get(f"/todos/{todo_id}", headers=headers)
        if current.status_code == 200:
            if_match = current.json()["updated_at"]
        else:
            # Row isn't visible to this caller; skip the auto-fetch and
            # let the PATCH surface its own 404 from the subscription
            # gate. Sending a stale placeholder would 409 before the
            # gate, hiding the 404 the test is asserting.
            if_match = None
    if if_match is not None:
        headers["If-Match"] = if_match
    return client.patch(f"/todos/{todo_id}", json=body, headers=headers)


def _get_todo(client: TestClient, todo_id: int, sub: str) -> dict[str, Any]:
    response = client.get(f"/todos/{todo_id}", headers=_auth(sub))
    assert response.status_code == 200
    return response.json()


# --- legal state transitions -----------------------------------------------


def test_patch_state_pending_to_in_progress_returns_200(
    client: TestClient,
) -> None:
    """AC: PATCH {state: "in_progress"} on `pending` -> 200."""
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})

    assert response.status_code == 200
    assert response.json()["state"] == "in_progress"


def test_patch_state_in_progress_to_done_returns_200(
    client: TestClient,
) -> None:
    """AC: PATCH {state: "done"} on `in_progress` -> 200."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "done"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "done"
    assert body["completed_at"] is not None
    assert body["completed_at"].endswith("Z")


def test_patch_state_pending_to_done_returns_200(client: TestClient) -> None:
    """`pending` -> `done` is a legal direct transition."""
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "done"})

    assert response.status_code == 200
    assert response.json()["state"] == "done"


def test_patch_state_in_progress_back_to_pending_returns_200(
    client: TestClient,
) -> None:
    """`in_progress` -> `pending` is freely reversible (ADR-0001)."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "pending"})

    assert response.status_code == 200
    assert response.json()["state"] == "pending"


def test_patch_state_in_progress_to_cancelled_returns_200(
    client: TestClient,
) -> None:
    """`in_progress` -> `cancelled` is a legal terminal transition."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "cancelled"})

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


# --- terminal-state rejection ---------------------------------------------


def test_patch_state_done_to_in_progress_returns_409(
    client: TestClient,
) -> None:
    """AC: PATCH {state: "in_progress"} on `done` -> 409 (terminal-state rejection)."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "done"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})

    assert response.status_code == 409


def test_patch_state_done_to_pending_returns_409(client: TestClient) -> None:
    """`done` is terminal; `done` -> `pending` is rejected."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "done"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "pending"})

    assert response.status_code == 409


def test_patch_state_cancelled_to_pending_returns_409(
    client: TestClient,
) -> None:
    """`cancelled` is terminal; only `deleted` is reachable from it."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "cancelled"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "pending"})

    assert response.status_code == 409


def test_patch_state_deleted_to_pending_returns_409(
    client: TestClient,
) -> None:
    """`deleted` has no outgoing edges; any transition is rejected."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "deleted"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "pending"})

    assert response.status_code == 409


# --- creator-only delete --------------------------------------------------


def test_patch_state_deleted_by_non_creator_returns_409(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: PATCH {state: "deleted"} by a non-creator -> 409 (creator-only)."""
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id)

    response = _patch(client, todo_id=todo_id, sub="bob", body={"state": "deleted"})

    assert response.status_code == 409


def test_patch_state_deleted_by_creator_returns_200(client: TestClient) -> None:
    """AC: PATCH {state: "deleted"} by the creator -> 200; `deleted_at` stamped."""
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "deleted"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "deleted"
    assert body["deleted_at"] is not None
    assert body["deleted_at"].endswith("Z")


def test_patch_state_done_to_deleted_by_creator_returns_200(
    client: TestClient,
) -> None:
    """`done` -> `deleted` is legal for the creator."""
    todo_id = _create(client, "alice")
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "done"})

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "deleted"})

    assert response.status_code == 200
    assert response.json()["state"] == "deleted"


def test_patch_state_done_to_deleted_by_non_creator_returns_409(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """`done` -> `deleted` is creator-only even though `deleted` is reachable."""
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id)
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "in_progress"})
    _patch(client, todo_id=todo_id, sub="alice", body={"state": "done"})

    response = _patch(client, todo_id=todo_id, sub="bob", body={"state": "deleted"})

    assert response.status_code == 409


# --- blocked_reason -------------------------------------------------------


def test_patch_blocked_reason_by_any_user_returns_200(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: PATCH {blocked_reason: "..."} by any user -> 200."""
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id)

    response = _patch(
        client,
        todo_id=todo_id,
        sub="bob",
        body={"blocked_reason": "waiting on review"},
    )

    assert response.status_code == 200
    assert response.json()["blocked_reason"] == "waiting on review"


def test_patch_blocked_reason_null_clears_value_returns_200(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """AC: setting `blocked_reason: null` -> 200; the field is cleared."""
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id)
    _patch(client, todo_id=todo_id, sub="bob", body={"blocked_reason": "x"})

    response = _patch(client, todo_id=todo_id, sub="bob", body={"blocked_reason": None})

    assert response.status_code == 200
    assert response.json()["blocked_reason"] is None


def test_patch_omitted_blocked_reason_keeps_value_returns_200(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """Omitting `blocked_reason` from the body leaves the stored value alone."""
    todo_id = _create(client, "alice")
    _insert_subscription(db, user_id="bob", todo_id=todo_id)
    _patch(client, todo_id=todo_id, sub="bob", body={"blocked_reason": "kept"})

    response = _patch(client, todo_id=todo_id, sub="bob", body={"title": "new"})

    assert response.status_code == 200
    body = response.json()
    assert body["blocked_reason"] == "kept"
    assert body["title"] == "new"


# --- If-Match --------------------------------------------------------------


def test_patch_without_if_match_returns_409(client: TestClient) -> None:
    """AC: PATCH without `If-Match` header -> 409."""
    todo_id = _create(client, "alice")

    headers = _auth("alice")
    headers.pop("If-Match", None)
    response = client.patch(f"/todos/{todo_id}", json={"title": "y"}, headers=headers)

    assert response.status_code == 409


def test_patch_refreshes_updated_at(client: TestClient) -> None:
    """AC: server refreshes `updated_at` on success."""
    todo_id = _create(client, "alice")
    initial_updated_at = _get_todo(client, todo_id, "alice")["updated_at"]

    # Sleep long enough for the server's wall-clock timestamp to differ.
    time.sleep(1.05)

    response = _patch(client, todo_id=todo_id, sub="alice", body={"title": "y"})

    assert response.status_code == 200
    new_updated_at = response.json()["updated_at"]
    assert new_updated_at != initial_updated_at


# --- If-Match value vs updated_at (T3b / #6) -------------------------------
#
# Optimistic concurrency: only the request carrying the latest `updated_at`
# in `If-Match` may mutate the row. Every other value -- including the
# *previous* `updated_at` from before a successful PATCH -- is rejected
# with `409 Conflict`. The atomic compare-and-swap is enforced at SQL
# level (`WHERE id = ? AND updated_at = ?`), so two writers that race
# with the same stale value get exactly one `200` and one `409`.


def test_patch_fresh_if_match_returns_200(client: TestClient) -> None:
    """AC: PATCH using `If-Match: "<updated_at_v1>"` -> 200.

    Captures the row's `updated_at_v1`, sends it back as `If-Match`, and
    expects the write to succeed. This is the baseline the other AC tests
    compare against.
    """
    todo_id = _create(client, "alice")
    updated_at_v1 = _get_todo(client, todo_id, "alice")["updated_at"]

    response = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "y"},
        if_match=updated_at_v1,
    )

    assert response.status_code == 200
    assert response.json()["title"] == "y"


def test_patch_stale_if_match_returns_409(client: TestClient) -> None:
    """AC: PATCH using a stale `If-Match` value -> 409.

    After a successful PATCH, the row's `updated_at` is refreshed. A second
    PATCH carrying the *previous* value must be rejected; the client is
    working from a stale read and shouldn't be allowed to silently overwrite.
    """
    todo_id = _create(client, "alice")
    updated_at_v1 = _get_todo(client, todo_id, "alice")["updated_at"]

    # Server timestamps are second-precision; sleep so the next write
    # lands on a fresh second and `updated_at` actually moves.
    time.sleep(1.05)

    # First write succeeds and refreshes `updated_at` -> v2.
    first = _patch(client, todo_id=todo_id, sub="alice", body={"title": "y"})
    assert first.status_code == 200
    updated_at_v2 = first.json()["updated_at"]
    assert updated_at_v2 != updated_at_v1

    # Second write with the *original* v1 header is now stale.
    response = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "z"},
        if_match=updated_at_v1,
    )

    assert response.status_code == 409


def test_patch_after_success_new_if_match_returns_200(client: TestClient) -> None:
    """AC: PATCH using the freshly-refreshed `If-Match` -> 200.

    The client reads `updated_at_v2` from the first successful response,
    then sends it back. The row is in sync; the write succeeds.
    """
    todo_id = _create(client, "alice")
    updated_at_v1 = _get_todo(client, todo_id, "alice")["updated_at"]

    time.sleep(1.05)  # ensure the first write lands on a fresh second

    first = _patch(client, todo_id=todo_id, sub="alice", body={"title": "y"})
    assert first.status_code == 200
    updated_at_v2 = first.json()["updated_at"]

    response = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "z"},
        if_match=updated_at_v2,
    )

    assert response.status_code == 200
    assert response.json()["title"] == "z"
    # The first read was an honest stale snapshot; the assertion above is
    # not just an artifact of timestamps ticking over.
    assert updated_at_v2 != updated_at_v1


def test_patch_refreshes_updated_at_visible_on_reread(client: TestClient) -> None:
    """AC: re-read after a successful PATCH shows a new `updated_at`.

    Captures `updated_at_v1` from a `GET`, sends a PATCH (carrying v1 as
    `If-Match`), then re-reads and asserts the timestamp differs. This is
    the same guarantee `test_patch_refreshes_updated_at` covers, framed
    through the If-Match flow that #6 makes load-bearing.
    """
    todo_id = _create(client, "alice")
    updated_at_v1 = _get_todo(client, todo_id, "alice")["updated_at"]

    time.sleep(1.05)  # server timestamps are second-precision

    patch_response = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "y"},
        if_match=updated_at_v1,
    )
    assert patch_response.status_code == 200
    updated_at_v2 = patch_response.json()["updated_at"]

    reread = _get_todo(client, todo_id, "alice")
    assert reread["updated_at"] == updated_at_v2
    assert reread["updated_at"] != updated_at_v1


def test_patch_two_concurrent_writes_one_200_one_409(
    client: TestClient,
) -> None:
    """AC: two PATCHes with the same `If-Match` -> exactly one `200`, one `409`.

    The SQL-level compare-and-swap (`WHERE id = ? AND updated_at = ?`) is
    what makes this guarantee hold even under true concurrency: the first
    writer to land flips `updated_at`, and the second writer's `UPDATE`
    matches zero rows. SQLite serialises writes inside a single
    connection, so a sequential replay is faithful to the production
    contract here.
    """
    todo_id = _create(client, "alice")
    updated_at_v1 = _get_todo(client, todo_id, "alice")["updated_at"]

    time.sleep(1.05)  # server timestamps are second-precision

    first = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "first"},
        if_match=updated_at_v1,
    )
    second = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "second"},
        if_match=updated_at_v1,
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409]

    # The successful write landed; the row carries "first" (the first
    # writer wins because the second writer's UPDATE matched zero rows).
    final = _get_todo(client, todo_id, "alice")
    assert final["title"] == "first"


def test_patch_arbitrary_if_match_value_returns_409(client: TestClient) -> None:
    """A random non-empty `If-Match` value is not a valid `updated_at`.

    The T3a ticket (#5) accepted any value as a stand-in; #6 closes that
    loophole. A client sending `If-Match: "anything"` against a real row
    is now a `409`, not a `200`.
    """
    todo_id = _create(client, "alice")

    response = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "y"},
        if_match="totally-wrong-value",
    )

    assert response.status_code == 409


def test_patch_if_match_precondition_is_independent_of_subscription_gate(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """The `If-Match` check fires after the subscription gate, not before.

    A non-subscriber still gets `404` (the gate hides the row first);
    an If-Match that doesn't match the row would have returned `409`,
    but the gate short-circuits before we ever compare. This guards
    against a regression where If-Match validation runs first and
    accidentally confirms the row exists to a non-subscriber.
    """
    todo_id = _create(client, "alice")
    updated_at_v1 = _get_todo(client, todo_id, "alice")["updated_at"]
    # bob has no subscription row, so the gate hides the todo.

    response = _patch(
        client,
        todo_id=todo_id,
        sub="bob",
        body={"title": "y"},
        if_match=updated_at_v1,
    )

    assert response.status_code == 404


# --- invalid body shape ---------------------------------------------------


def test_patch_title_empty_string_returns_422(client: TestClient) -> None:
    """AC: PATCH {title: ""} -> 422."""
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="alice", body={"title": ""})

    assert response.status_code == 422


def test_patch_title_null_returns_422(client: TestClient) -> None:
    """`title: null` is not a valid title; must return 422, not silently clear."""
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="alice", body={"title": None})

    assert response.status_code == 422


def test_patch_unknown_state_string_returns_422(client: TestClient) -> None:
    """`state: "garbage"` is not a valid enum value; rejected at the body parser."""
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="alice", body={"state": "garbage"})

    assert response.status_code == 422


def test_patch_non_string_title_returns_422(client: TestClient) -> None:
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="alice", body={"title": 42})

    assert response.status_code == 422


# --- combined updates -----------------------------------------------------


def test_patch_combined_title_and_state_returns_200(client: TestClient) -> None:
    """Title and state in one body both apply, and `updated_at` is refreshed once."""
    todo_id = _create(client, "alice")
    initial_updated_at = _get_todo(client, todo_id, "alice")["updated_at"]
    time.sleep(1.05)

    response = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "renamed", "state": "in_progress"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "renamed"
    assert body["state"] == "in_progress"
    assert body["updated_at"] != initial_updated_at


# --- access checks --------------------------------------------------------


def test_patch_without_auth_returns_401(client: TestClient) -> None:
    todo_id = _create(client, "alice")

    response = client.patch(f"/todos/{todo_id}", json={"title": "y"})

    assert response.status_code == 401


def test_patch_with_invalid_jwt_returns_401(client: TestClient) -> None:
    todo_id = _create(client, "alice")

    response = client.patch(
        f"/todos/{todo_id}",
        json={"title": "y"},
        headers={"Authorization": "Bearer not.a.real.jwt", "If-Match": "x"},
    )

    assert response.status_code == 401


def test_patch_by_non_subscriber_returns_404(client: TestClient) -> None:
    """The subscription gate hides the todo before state-machine logic runs."""
    todo_id = _create(client, "alice")

    response = _patch(client, todo_id=todo_id, sub="bob", body={"title": "y"})

    assert response.status_code == 404


def test_patch_on_missing_todo_returns_404(client: TestClient) -> None:
    """Non-existent id collapses to 404 with the same envelope as no-access."""
    response = _patch(client, todo_id=99999, sub="alice", body={"title": "y"})

    assert response.status_code == 404