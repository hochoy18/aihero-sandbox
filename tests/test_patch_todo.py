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


# --- helpers ---------------------------------------------------------------


def _create(client: TestClient, sub: str, title: str = "x") -> int:
    token = make_jwt(sub=sub)
    response = client.post(
        "/todos",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _auth(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(sub=sub)}"}


def _insert_subscription(
    db: sqlite3.Connection, *, user_id: str, todo_id: int, position: int = 1
) -> None:
    db.execute(
        "INSERT INTO user_todo_views "
        "(user_id, todo_id, position, subscribed_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, todo_id, position, "2026-09-24T00:00:00Z"),
    )
    db.commit()


def _patch(
    client: TestClient,
    *,
    todo_id: int,
    sub: str,
    body: dict[str, Any],
    if_match: str | None = "anything",
) -> Any:
    headers = _auth(sub)
    if if_match is not None:
        headers["If-Match"] = if_match
    return client.patch(f"/todos/{todo_id}", json=body, headers=headers)


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


def test_patch_if_match_value_is_accepted_regardless(client: TestClient) -> None:
    """AC: any non-empty `If-Match` value is accepted in this ticket.

    Validation of the header against the row's `updated_at` lands in T3b (#6).
    """
    todo_id = _create(client, "alice")

    response = _patch(
        client,
        todo_id=todo_id,
        sub="alice",
        body={"title": "y"},
        if_match="totally-wrong-value",
    )

    assert response.status_code == 200
    assert response.json()["title"] == "y"


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


def _get_todo(client: TestClient, todo_id: int, sub: str) -> dict[str, Any]:
    response = client.get(f"/todos/{todo_id}", headers=_auth(sub))
    assert response.status_code == 200
    return response.json()


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