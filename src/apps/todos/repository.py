"""SQLite-backed persistence for the `todos` table.

Issue #3 acceptance criterion: "A row exists in the SQLite `todos` table
after creation." This module is the persistence side of that contract. The
HTTP layer never touches a connection directly — `TodoRepository` takes
one per call.

The repository does not depend on the domain layer's `TodoState` enum: the DB
stores `state` as a plain TEXT and the repository hands back a raw string.
`TodoService` translates strings to enums in `Todo.from_row`. This keeps
the dependency direction one-way (domain -> persistence, not the reverse).

Issue #5 adds `update()`: a partial-update method that the service layer
calls once it has resolved the state-machine and creator-only rules. The
repository stamps `updated_at` itself; the service layer also sets
`completed_at` / `deleted_at` via the same `update()` call when the new
state implies those timestamps, so callers see exactly one write per
PATCH.

Issue #6 threads optimistic concurrency through `update()`: every write
must carry the `updated_at` the client read, and the SQL `UPDATE` is
guarded by `WHERE id = ? AND updated_at = ?`. A `rowcount` of zero means
the precondition failed (the row was changed by another writer between
the client's read and this write); the repository returns `None` and the
service translates that into a domain error.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

# Constant lives here, not in the domain module, so the repository has no
# inbound dependency on domain. The domain layer imports the same literal
# as its canonical enum value.
TODO_STATE_PENDING: Final = "pending"


def utcnow_iso() -> str:
    """Server-set timestamp in ISO 8601 with a trailing Z.

    Clients cannot override timestamps — this is the single source of `now()`.

    Microsecond precision matters for optimistic concurrency (#6):
    `updated_at` is the compare-and-swap token, and two PATCHes that land
    inside the same second would otherwise collide on the same string
    and both succeed against `WHERE updated_at = ?`. The tracer bullet
    used second-precision for diff-friendly test fixtures; #6 promotes
    this to microseconds so the CAS is unique at sub-second scale.

    Public so the domain layer can stamp `completed_at` and `deleted_at`
    when transitioning to terminal states without going through SQL.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Sentinel mirroring the domain layer's `UNSET`. Lives here so the
# repository doesn't import the domain module (the dependency arrow goes
# domain -> persistence, not the reverse). The domain layer passes this
# same sentinel through `UpdateTodoInput`; the repository treats anything
# equal to `UNSET` as "no change for this column".
class _UnsetType:
    def __repr__(self) -> str:
        return "<UNSET>"

    def __bool__(self) -> bool:
        return False


UNSET: Final[_UnsetType] = _UnsetType()


@dataclass(frozen=True)
class TodoRow:
    """A raw row from `todos`, as stored.

    `state` is a plain string — the domain layer turns it into `TodoState`.
    `description` is empty-string for "no description"; the service maps
    that back to `None` in the API shape.
    """

    id: int
    title: str
    description: str
    state: str
    created_by: str
    created_at: str
    updated_at: str
    completed_at: str | None
    deleted_at: str | None
    blocked_reason: str | None


class TodoRepository:
    """SQLite-backed repository over the `todos` table.

    Each method takes a connection explicitly so the HTTP route's per-request
    `get_db` dependency flows through unchanged — there's no module-level
    connection state to mock.
    """

    def create(self, conn: sqlite3.Connection, *, title: str, description: str, created_by: str) -> TodoRow:
        """Insert a new todo and return the persisted row.

        `state` is set to `pending`; `created_at` and `updated_at` are
        stamped server-side to the same instant so the row's edit history
        starts consistent.
        """
        now = utcnow_iso()
        cursor = conn.execute(
            """
            INSERT INTO todos
                (title, description, state, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, description, TODO_STATE_PENDING, created_by, now, now),
        )
        conn.commit()
        return self._get_by_id(conn, cursor.lastrowid)

    def get_by_id(self, conn: sqlite3.Connection, todo_id: int) -> TodoRow | None:
        return self._get_by_id(conn, todo_id)

    def update(
        self,
        conn: sqlite3.Connection,
        *,
        todo_id: int,
        expected_updated_at: str,
        title: Any = UNSET,
        description: Any = UNSET,
        state: Any = UNSET,
        blocked_reason: Any = UNSET,
        completed_at: Any = UNSET,
        deleted_at: Any = UNSET,
    ) -> TodoRow | None:
        """Apply a partial update and return the persisted row.

        Columns whose argument is `UNSET` are not written; everything else
        is. `updated_at` is always refreshed (the server is the single
        source of `now()`). The caller — `TodoService.update` — resolves
        the state-machine and creator-only rules before invoking this, so
        by the time we get here the columns are guaranteed to be valid.

        `completed_at` and `deleted_at` are stamped by the service layer
        when the new state implies those timestamps, so they share this
        single SQL statement and a single transaction.

        Issue #6 — optimistic concurrency: the SQL `UPDATE` is guarded by
        `WHERE id = ? AND updated_at = ?`. If the client's `expected_updated_at`
        no longer matches the row (someone else wrote first), zero rows are
        affected and the method returns `None`. The caller is responsible
        for translating `None` into a domain-level precondition failure;
        the repository stays HTTP-agnostic.

        The check is at SQL level on purpose: a Python-side read-then-write
        would race with another writer between the read and the write.
        Wrapping both the precondition and the write in a single SQL
        statement makes the guarantee atomic.
        """
        assignments: list[str] = []
        params: list[Any] = []

        if title is not UNSET:
            assignments.append("title = ?")
            params.append(title)
        if description is not UNSET:
            # Storage uses `''` for "no description"; `None` becomes that.
            assignments.append("description = ?")
            params.append(description if description is not None else "")
        if state is not UNSET:
            assignments.append("state = ?")
            params.append(state)
        if blocked_reason is not UNSET:
            assignments.append("blocked_reason = ?")
            params.append(blocked_reason)
        if completed_at is not UNSET:
            assignments.append("completed_at = ?")
            params.append(completed_at)
        if deleted_at is not UNSET:
            assignments.append("deleted_at = ?")
            params.append(deleted_at)

        # `updated_at` is always refreshed — the server is the sole writer.
        now = utcnow_iso()
        assignments.append("updated_at = ?")
        params.append(now)

        params.append(todo_id)
        params.append(expected_updated_at)
        cursor = conn.execute(
            f"UPDATE todos SET {', '.join(assignments)} "
            "WHERE id = ? AND updated_at = ?",
            params,
        )
        conn.commit()
        # `cursor.rowcount` is the number of rows the UPDATE actually
        # matched. Zero means the precondition failed (or the row was
        # removed); both collapse to "precondition not met" at this seam.
        if cursor.rowcount == 0:
            return None
        return self._get_by_id(conn, todo_id)

    def _get_by_id(self, conn: sqlite3.Connection, todo_id: int) -> TodoRow | None:
        row = conn.execute(
            """
            SELECT id, title, description, state, created_by,
                   created_at, updated_at, completed_at, deleted_at,
                   blocked_reason
            FROM todos
            WHERE id = ?
            """,
            (todo_id,),
        ).fetchone()
        if row is None:
            return None
        return TodoRow(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            state=row["state"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            deleted_at=row["deleted_at"],
            blocked_reason=row["blocked_reason"],
        )


class UserTodoViewRepository:
    """SQLite-backed repository over the `user_todo_views` table.

    Issue #4 introduces the subscription gate: `GET /todos/{id}` must check
    that the caller has a row here (or is the todo's creator) before
    returning the todo. The creator-bypass decision lives in `TodoService`,
    not here — this repository answers only "does this user have a view of
    this todo?"

    Subscribe, unsubscribe, list-by-user, and reorder methods (issue #9)
    will land here in a follow-up ticket.
    """

    def has_view(
        self, conn: sqlite3.Connection, *, user_id: str, todo_id: int
    ) -> bool:
        """Return `True` iff `user_id` has a `user_todo_views` row for `todo_id`.

        Existence is the only signal the service needs — `position` and
        `subscribed_at` are not part of the read-gate check.
        """
        row = conn.execute(
            "SELECT 1 FROM user_todo_views WHERE user_id = ? AND todo_id = ?",
            (user_id, todo_id),
        ).fetchone()
        return row is not None


__all__ = [
    "TodoRepository",
    "TodoRow",
    "UserTodoViewRepository",
    "UNSET",
]