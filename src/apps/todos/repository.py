"""SQLite-backed persistence for the `todos` table.

Issue #3 acceptance criterion: "A row exists in the SQLite `todos` table
after creation." This module is the persistence side of that contract. The
HTTP layer never touches a connection directly — `TodoRepository` takes
one per call.

The repository does not depend on the domain layer's `TodoState` enum: the DB
stores `state` as a plain TEXT and the repository hands back a raw string.
`TodoService` translates strings to enums in `Todo.from_row`. This keeps
the dependency direction one-way (domain -> persistence, not the reverse).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# Constant lives here, not in the domain module, so the repository has no
# inbound dependency on domain. The domain layer imports the same literal
# as its canonical enum value.
TODO_STATE_PENDING = "pending"


def _utcnow_iso() -> str:
    """Server-set timestamp in ISO 8601 with a trailing Z.

    Clients cannot override timestamps — this is the single source of `now()`.
    Microseconds are dropped to match the tracer-bullet fixture format and
    keep responses diff-friendly in tests.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        now = _utcnow_iso()
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


__all__ = ["TodoRepository", "TodoRow", "UserTodoViewRepository"]