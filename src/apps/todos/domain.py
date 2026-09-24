"""Domain types and the `TodoService` façade.

Issue #3 acceptance criteria, expressed as domain rules:
  * `created_by` is set from the authenticated caller's JWT `sub`.
  * `state` defaults to `pending` on creation; `description` and
    `blocked_reason` default to `None` ("no description / not blocked").
  * A blank `title` is a domain-level validation error, surfaced as `422`
    by the HTTP layer.

Issue #4 adds the subscription gate (`get_for_user`): a user can read a
todo via `GET /todos/{id}` only if they are the creator, or they have a
`user_todo_views` row. Either failure is surfaced as `404` by the HTTP
layer — there is no distinction between "doesn't exist" and "exists but
no access", to avoid leaking existence.

The domain depends only on the persistence layer (no FastAPI, no HTTP
status codes). The HTTP layer maps domain errors into status codes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum

from .repository import (
    TODO_STATE_PENDING,
    TodoRepository,
    TodoRow,
    UserTodoViewRepository,
)


class TodoState(str, Enum):
    PENDING = TODO_STATE_PENDING
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class InvalidTitleError(ValueError):
    """Raised when a create/update body has no usable `title`."""


@dataclass(frozen=True)
class Todo:
    """The API-shaped Todo.

    `description` is `None` when there's no description; the storage layer
    uses an empty string and the service translates. `completed_at` and
    `deleted_at` stay `None` until the state machine reaches `done` or
    `deleted`.
    """

    id: int
    title: str
    description: str | None
    state: TodoState
    created_by: str
    created_at: str
    updated_at: str
    completed_at: str | None
    deleted_at: str | None
    blocked_reason: str | None

    @classmethod
    def from_row(cls, row: TodoRow) -> "Todo":
        return cls(
            id=row.id,
            title=row.title,
            description=row.description or None,
            state=TodoState(row.state),
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
            deleted_at=row.deleted_at,
            blocked_reason=row.blocked_reason,
        )


@dataclass(frozen=True)
class CreateTodoInput:
    """Validated create payload from the HTTP layer."""

    title: str
    description: str


class TodoService:
    """The canonical façade. Owns the rules; persistence is delegated.

    Each call takes a `conn` so the HTTP route's per-request `get_db`
    dependency flows straight through to the repository. The service holds
    no connection state of its own.
    """

    def __init__(
        self,
        todos: TodoRepository,
        views: UserTodoViewRepository,
    ) -> None:
        self._todos = todos
        self._views = views

    def create(
        self,
        conn: sqlite3.Connection,
        *,
        created_by: str,
        input: CreateTodoInput,
    ) -> Todo:
        """Create a todo belonging to `created_by` and return the persisted row.

        Raises `InvalidTitleError` when `title` is missing or empty. The HTTP
        layer maps that to a 422; everything else here is a 500-class
        internal error and should not be reachable from the API contract.
        """
        title = input.title.strip()
        if not title:
            raise InvalidTitleError("title must be a non-empty string")
        row = self._todos.create(
            conn,
            title=title,
            description=input.description,
            created_by=created_by,
        )
        return Todo.from_row(row)

    def get_for_user(
        self,
        conn: sqlite3.Connection,
        *,
        todo_id: int,
        user_id: str,
    ) -> Todo | None:
        """Fetch a todo for `user_id`, enforcing the subscription gate.

        Returns the `Todo` when either:
          * the todo doesn't exist → `None` (HTTP layer renders `404`), or
          * the caller is the creator (creator bypass — no subscription row
            required), or
          * the caller has a `user_todo_views` row for this todo.

        Returns `None` in every other situation. There is no separate "exists
        but no access" signal — the two cases must collapse to the same
        response so the API doesn't leak existence (ADR-0003).
        """
        row = self._todos.get_by_id(conn, todo_id)
        todo = Todo.from_row(row) if row is not None else None
        if todo is None:
            return None
        if todo.created_by == user_id:
            return todo
        if self._views.has_view(conn, user_id=user_id, todo_id=todo_id):
            return todo
        return None


__all__ = [
    "CreateTodoInput",
    "InvalidTitleError",
    "Todo",
    "TodoService",
    "TodoState",
]