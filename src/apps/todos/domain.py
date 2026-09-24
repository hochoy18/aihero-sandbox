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

Issue #5 adds `update()`: the state machine (ADR-0001) plus creator-only
soft-delete. The HTTP layer is responsible for `If-Match`; the domain is
responsible for transitions, creator-only rules, and title validation.

Issue #6 threads optimistic concurrency through `update()`: every write
must carry the `updated_at` the caller read, and the repository enforces
that with an SQL `WHERE updated_at = ?` clause. A precondition failure
becomes `StalePreconditionError` here, which the HTTP layer maps to
`409 Conflict`. The same seam will be reused by the DELETE path (T7).

Issue #7 adds `subscribe` / `unsubscribe` / `list_for_user`: subscribe
assigns `position = max(user.position) + 1`, refuses to double-subscribe,
and lists the user's view in `position` order. The HTTP layer still owns
the read gate (404 vs 200).

The domain depends only on the persistence layer (no FastAPI, no HTTP
status codes). The HTTP layer maps domain errors into status codes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from .repository import (
    TODO_STATE_PENDING,
    UNSET,
    TodoRepository,
    TodoRow,
    UserTodoViewRepository,
    utcnow_iso,
)


class TodoState(str, Enum):
    PENDING = TODO_STATE_PENDING
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
    DELETED = "deleted"


# State machine (ADR-0001).
#
#   pending        -> in_progress, done, cancelled, deleted
#   in_progress    -> pending, done, cancelled, deleted
#   done           -> deleted       (terminal except for delete)
#   cancelled      -> deleted       (terminal except for delete)
#   deleted        -> {}            (no outgoing edges)
#
# Transitions to `deleted` are creator-only; every other legal transition
# is open to any user with access (the subscription gate is the HTTP layer's
# job, not the domain's).
_LEGAL_TRANSITIONS: Final[dict[TodoState, frozenset[TodoState]]] = {
    TodoState.PENDING: frozenset({
        TodoState.IN_PROGRESS,
        TodoState.DONE,
        TodoState.CANCELLED,
        TodoState.DELETED,
    }),
    TodoState.IN_PROGRESS: frozenset({
        TodoState.PENDING,
        TodoState.DONE,
        TodoState.CANCELLED,
        TodoState.DELETED,
    }),
    TodoState.DONE: frozenset({TodoState.DELETED}),
    TodoState.CANCELLED: frozenset({TodoState.DELETED}),
    TodoState.DELETED: frozenset(),
}


# Default states surfaced by `GET /todos` (issue #8). The spec's "active-only"
# default excludes the terminal states (`done`, `cancelled`) and the
# soft-deleted state (`deleted`) — the list is "what should the user
# action today?". `?state=...` overrides this set; absent an override, the
# route passes `None` and the service substitutes `DEFAULT_ACTIVE_STATES`.
DEFAULT_ACTIVE_STATES: Final[frozenset[TodoState]] = frozenset({
    TodoState.PENDING,
    TodoState.IN_PROGRESS,
})


def is_legal_transition(from_state: TodoState, to_state: TodoState) -> bool:
    """True iff the `from_state -> to_state` edge is in the state machine."""
    return to_state in _LEGAL_TRANSITIONS[from_state]


def is_creator_only_target(to_state: TodoState) -> bool:
    """True iff reaching `to_state` requires the creator's identity.

    Named for the target (not the source/target pair) because every state
    can transition *to* `deleted`, so the gating question is solely
    "does the actor have to be the creator to land here?"
    """
    return to_state is TodoState.DELETED


class InvalidTitleError(ValueError):
    """Raised when a create/update body has no usable `title`."""


class InvalidStateTransitionError(Exception):
    """Raised when a requested state transition isn't in the state machine.

    Carries the source and target states so the HTTP layer can shape a
    useful 409 response without re-deriving them.
    """

    def __init__(self, from_state: TodoState, to_state: TodoState) -> None:
        super().__init__(
            f"cannot transition from {from_state.value} to {to_state.value}"
        )
        self.from_state = from_state
        self.to_state = to_state


class NotCreatorError(Exception):
    """Raised when a non-creator attempts a creator-only operation (delete)."""


class StalePreconditionError(Exception):
    """Raised when the caller's `If-Match` value no longer matches the row.

    Indicates optimistic-concurrency loss: the `updated_at` the caller
    read has been overwritten by another writer between the read and
    this write. The HTTP layer translates this to `409 Conflict`.

    The error is raised by `TodoService.update` only — the repository
    signals the same condition by returning `None`, and the service is
    the layer that turns a SQL rowcount of zero into a domain concept.
    """


class TodoNotFoundError(Exception):
    """Raised when a Todo referenced by id doesn't exist.

    Distinct from "exists but no access" — the read gate (`get_for_user`)
    conflates those into a single `None` return for `GET /todos/{id}`,
    but `subscribe` needs to tell them apart. A subscription to a
    missing todo is a `404`; subscribing without access is fine
    (anyone authenticated can subscribe to anything), so that case
    doesn't exist. The HTTP layer maps this to `404 Not Found`.
    """


class AlreadySubscribedError(Exception):
    """Raised when a user tries to subscribe to a todo they're already on.

    The `(user_id, todo_id)` primary key on `user_todo_views` would
    raise `sqlite3.IntegrityError` on a duplicate insert; the service
    pre-checks via `has_view` to translate that into a domain concept.
    The HTTP layer maps this to `422 Unprocessable Entity` — the
    request is well-formed but semantically wrong (you can't join
    twice).
    """


# `UNSET` is the canonical sentinel for "field was not included in the
# PATCH body" — re-exported from the persistence layer so the domain and
# the repository share the same instance. The HTTP layer uses Pydantic's
# `exclude_unset=True` to populate only the fields the client actually
# sent; this sentinel keeps the boundary crisp — `None` carries domain
# meaning (clear / set-to-null) and "unset" carries the no-op meaning.


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


@dataclass(frozen=True)
class UpdateTodoInput:
    """Validated update payload from the HTTP layer.

    Each field's default is `UNSET`, meaning "field was not in the PATCH
    body — leave the stored value alone." Setting a field to `None`
    carries domain meaning (e.g. `blocked_reason=None` clears the
    reason, `description=None` clears the description). `title=None` and
    `state=None` are domain errors (raised by `TodoService.update`).
    """

    title: Any = UNSET
    description: Any = UNSET
    state: Any = UNSET
    blocked_reason: Any = UNSET


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

    def update(
        self,
        conn: sqlite3.Connection,
        *,
        todo_id: int,
        actor_id: str,
        expected_updated_at: str,
        input: UpdateTodoInput,
    ) -> Todo:
        """Apply `input` to the row, enforcing the state machine and
        creator-only rules.

        Returns the persisted `Todo` after the update. Raises:
          * `InvalidTitleError` — `title` is empty/whitespace.
          * `InvalidStateTransitionError` — the requested state transition
            is not in the state machine (ADR-0001).
          * `NotCreatorError` — the caller is not the creator but is trying
            to transition to `deleted`.
          * `StalePreconditionError` — `expected_updated_at` does not match
            the row's current `updated_at`. The HTTP layer maps this to
            `409 Conflict`; the client must re-read and retry.

        Subscription gating (404) is the HTTP layer's job; this method
        trusts the caller has access. The `expected_updated_at` value is
        the verbatim `If-Match` header the client sent — the repository's
        SQL is what actually enforces the comparison, so this method
        doesn't peek at the row before the write.
        """
        # Resolve the requested state (if any) and validate the transition
        # before touching any field — fail closed. We compute the eventual
        # `state` column write and the side-effect timestamps
        # (`completed_at`, `deleted_at`) up front so the repository call
        # below stays a single SQL statement. The row's current state
        # comes from a single read inside the same `conn` so SQLite sees
        # the transaction begin here.
        row = self._todos.get_by_id(conn, todo_id)
        todo = Todo.from_row(row)
        new_state: TodoState | None = None
        new_completed_at: Any = UNSET
        new_deleted_at: Any = UNSET
        if input.state is not UNSET:
            if not isinstance(input.state, TodoState):
                # The HTTP layer's Pydantic model should have caught this.
                # Defending in depth keeps the domain rule independent of
                # the transport.
                raise ValueError("state must be a TodoState")
            new_state = input.state
            if new_state is not todo.state:
                if not is_legal_transition(todo.state, new_state):
                    raise InvalidStateTransitionError(todo.state, new_state)
                if (
                    is_creator_only_target(new_state)
                    and todo.created_by != actor_id
                ):
                    raise NotCreatorError(
                        "only the creator can transition a todo to deleted"
                    )
                # Side-effect timestamps: stamp `now()` when we *enter* a
                # terminal state. We never overwrite an existing stamp on
                # a subsequent, no-op write.
                if new_state is TodoState.DONE:
                    new_completed_at = utcnow_iso()
                if new_state is TodoState.DELETED:
                    new_deleted_at = utcnow_iso()

        # Validate and resolve the new title (if any).
        new_title: Any = UNSET
        if input.title is not UNSET:
            if input.title is None or not isinstance(input.title, str):
                raise InvalidTitleError("title must be a non-empty string")
            stripped = input.title.strip()
            if not stripped:
                raise InvalidTitleError("title must be a non-empty string")
            new_title = stripped

        # Description is freely writable. The repository stores it as an
        # empty string when `None`; the API layer keeps the natural shape.
        new_description: Any = UNSET
        if input.description is not UNSET:
            new_description = input.description

        # `blocked_reason` accepts a string or `None` (which clears it).
        new_blocked_reason: Any = UNSET
        if input.blocked_reason is not UNSET:
            new_blocked_reason = input.blocked_reason

        updated = self._todos.update(
            conn,
            todo_id=todo_id,
            expected_updated_at=expected_updated_at,
            title=new_title,
            description=new_description,
            state=new_state.value if new_state is not None else UNSET,
            blocked_reason=new_blocked_reason,
            completed_at=new_completed_at,
            deleted_at=new_deleted_at,
        )
        if updated is None:
            # The atomic SQL UPDATE matched zero rows: either the row was
            # removed (not possible today) or — the only path that can
            # fire here — `expected_updated_at` no longer matches the
            # row. Surface that as a domain concept so the HTTP layer
            # can shape the 409 response.
            raise StalePreconditionError(
                "If-Match value does not match the row's updated_at"
            )
        return Todo.from_row(updated)

    def subscribe(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        todo_id: int,
    ) -> None:
        """Subscribe `user_id` to `todo_id` at `max(user.position) + 1`.

        Raises:
          * `TodoNotFoundError` — no row with that id.
          * `AlreadySubscribedError` — `user_id` already has a view row
            for this todo. The unique `(user_id, todo_id)` primary key
            would also catch this at SQL level; we pre-check so the
            HTTP layer sees a domain concept (`422`) instead of an
            `sqlite3.IntegrityError`, AND catch `IntegrityError` as a
            belt-and-suspenders fallback for the race window between
            the pre-check and the insert (two concurrent subscribes
            from the same user would both pass `has_view` and then
            collide at the unique PK).

        Position rule: `max(user_id's existing positions) + 1`, with
        `max over zero rows = 0` so the first subscription lands at
        `1`. The rule is per-user, not global — Alice's high position
        does not bump Bob's first subscription.

        Concurrency note: two simultaneous subscribes for *different*
        `todo_id`s can both read `max = N` and both write `N + 1`,
        producing duplicate positions. The position column is not
        unique by spec — it only orders the view — so this is benign
        (ties are resolved arbitrarily) and a transactional SELECT
        MAX + INSERT is left for a future optimisation.
        """
        if self._todos.get_by_id(conn, todo_id) is None:
            raise TodoNotFoundError(
                f"todo {todo_id} does not exist"
            )
        if self._views.has_view(conn, user_id=user_id, todo_id=todo_id):
            raise AlreadySubscribedError(
                f"user {user_id} is already subscribed to todo {todo_id}"
            )
        next_position = self._views.max_position(conn, user_id=user_id) + 1
        try:
            self._views.subscribe(
                conn,
                user_id=user_id,
                todo_id=todo_id,
                position=next_position,
                subscribed_at=utcnow_iso(),
            )
        except sqlite3.IntegrityError as exc:
            # Two concurrent subscribes from the same user to the same
            # todo both passed `has_view` and one lost the race to the
            # unique `(user_id, todo_id)` PK. Translate the SQL failure
            # into the same domain error the pre-check would have
            # raised, so the HTTP layer can shape the 422 the same way.
            raise AlreadySubscribedError(
                f"user {user_id} is already subscribed to todo {todo_id}"
            ) from exc

    def unsubscribe(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        todo_id: int,
    ) -> None:
        """Remove `user_id`'s view row for `todo_id` if it exists.

        Idempotent at this layer: calling unsubscribe on a user who
        isn't subscribed (or on a non-existent todo) is a no-op. The
        repository's `unsubscribe` returns a bool the service discards;
        the HTTP layer translates "idempotent" into `204` regardless.
        """
        self._views.unsubscribe(conn, user_id=user_id, todo_id=todo_id)

    def list_for_user(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        states: frozenset[TodoState] | None = None,
    ) -> list[Todo]:
        """Return the `Todo`s `user_id` is subscribed to, by position ASC.

        Joins in Python: the views table holds ids, the todos table
        holds the rows, so the service glues them. The N+1 here is
        acceptable for MVP — the per-user list is bounded by what one
        person has chosen to track, and a single `IN (...)` fetch is
        an easy follow-up if profiles get large.

        Filtering by `state` (issue #8): when `states` is `None`, the
        default-active set (`pending`, `in_progress`) is used so the
        route's "no `?state`" behaviour surfaces only what the user
        should action today. Pass an explicit set to override.

        Ordering comes from the views table's `position`, so the
        filter never re-orders the result: we read ids in position
        order, then drop ids whose `state` isn't in the requested set.
        """
        effective_states = (
            DEFAULT_ACTIVE_STATES if states is None else states
        )
        todo_ids = self._views.list_todo_ids_for_user(conn, user_id=user_id)
        todos: list[Todo] = []
        for todo_id in todo_ids:
            row = self._todos.get_by_id(conn, todo_id)
            if row is None:
                continue
            if TodoState(row.state) not in effective_states:
                continue
            todos.append(Todo.from_row(row))
        return todos


__all__ = [
    "AlreadySubscribedError",
    "CreateTodoInput",
    "InvalidStateTransitionError",
    "InvalidTitleError",
    "NotCreatorError",
    "StalePreconditionError",
    "Todo",
    "TodoNotFoundError",
    "TodoService",
    "TodoState",
    "UNSET",
    "UpdateTodoInput",
    "is_creator_only_target",
    "is_legal_transition",
]