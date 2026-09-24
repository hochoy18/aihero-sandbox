"""HTTP application factory and test-client wiring.

Production path: `create_app()` opens one in-memory SQLite connection during
the FastAPI lifespan and shares it across every request. Writes survive within
the process lifetime; persistence across restarts is left to a later ticket.

Test path: `create_app(db_factory=...)` short-circuits the lifespan. The
factory is called per request, so tests can share a single connection
(`make_client(db)`) across the whole test.

Routes owned by issue #3:
  * `POST /todos`              — create a todo as the authenticated caller.

Routes owned by issue #4:
  * `GET  /todos/{id}`         — creator bypass + subscription gate; the
                                 404 envelope is identical for non-existent
                                 ids and non-subscribers (ADR-0003).

Routes owned by issue #5:
  * `PATCH /todos/{id}`        — state machine + creator-only delete,
                                 freely-writable `blocked_reason`, and a
                                 required `If-Match` header.

Routes owned by issue #6:
  * `PATCH /todos/{id}`        — `If-Match` value is validated against the
                                 row's current `updated_at` (T3b).
  * `require_if_match`         — reusable FastAPI dependency: rejects
                                 requests with a missing `If-Match` header
                                 with `409`. Built here so T7's `DELETE`
                                 path can attach the same dependency
                                 without duplicating the header check.

Routes owned by issue #7:
  * `POST   /todos/{id}/subscribe` — add a `user_todo_views` row at
                                     `max(user.position) + 1`.
  * `DELETE /todos/{id}/subscribe` — remove the row; idempotent.
  * `GET    /todos`                — list the caller's subscribed todos
                                     in `position` order. State filtering
                                     and the default-active behaviour
                                     land in #8.

Routes owned by issue #8:
  * `GET    /todos`                — `?state=...` query parameter
                                     accepts one or more comma-separated
                                     `TodoState` values; absent the
                                     parameter, the route filters to the
                                     active set (`pending`, `in_progress`).
                                     Invalid state names yield `422`.

Tracers left by issue #2:
  * (none — the tracer list endpoint grew up into the real one in #7)
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from .auth import AuthUser, verify_jwt
from .db import make_db
from .domain import (
    AlreadySubscribedError,
    CreateTodoInput,
    InvalidStateTransitionError,
    InvalidTitleError,
    NotCreatorError,
    StalePreconditionError,
    Todo,
    TodoNotFoundError,
    TodoService,
    TodoState,
    UpdateTodoInput,
)
from .repository import TodoRepository, UserTodoViewRepository

DBFactory = Callable[[], sqlite3.Connection]


class CreateTodoBody(BaseModel):
    """`POST /todos` request body.

    Shape validation only: `title` must be a string, `description` optional.
    The non-blank rule for `title` lives in `TodoService` (the domain), so
    it is enforced through a single path and translates to a single 422
    response in the route.

    Defined at module scope so Pydantic can resolve the forward reference
    that FastAPI builds for body parameters.
    """

    title: str
    description: str | None = None


class UpdateTodoBody(BaseModel):
    """`PATCH /todos/{id}` request body.

    Every field is optional; an explicit `null` is distinct from a missing
    key, so `model_dump(exclude_unset=True)` lets the route tell the
    service which fields to touch. The service layer's `UNSET` sentinel
    carries the "no change" meaning; a literal `None` carries the
    "clear this field" meaning where the domain allows it (e.g.
    `blocked_reason`).

    `state` uses Pydantic's enum validation so a garbage string fails
    here with a 422 instead of reaching the state machine.
    """

    title: str | None = None
    description: str | None = None
    state: TodoState | None = None
    blocked_reason: str | None = None


def require_if_match(
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> str:
    """Reject requests with a missing `If-Match` header.

    Reusable FastAPI dependency for routes that mutate a Todo (PATCH,
    and the future DELETE in T7). Returns the header value so the route
    can pass it on to the domain layer; raises `409 Conflict` when the
    header is absent or empty.

    Note: this dependency only enforces *presence*. The actual
    optimistic-concurrency comparison against `updated_at` happens at
    SQL level inside the service layer — that's what makes the check
    atomic, so two racing writers can't both pass the HTTP-layer test.

    Routes that attach this dependency should also attach their own
    access gate *earlier in the function signature*, so callers who
    shouldn't see the row receive the gate's `404` before `409` from
    a missing `If-Match`. Otherwise a missing header would
    accidentally confirm the row's existence to a caller who has no
    read access.
    """
    if not if_match:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="If-Match header is required",
        )
    return if_match


def create_app(db_factory: DBFactory | None = None) -> FastAPI:
    """Build a FastAPI app. Pass `db_factory` to inject a test connection."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if db_factory is None:
            # Production path: one shared in-memory connection for the
            # lifetime of the process.
            app.state.db = make_db()
            try:
                yield
            finally:
                app.state.db.close()
        else:
            # Test path: the caller owns the connection; we don't open one.
            yield

    app = FastAPI(title="aihero-todos", lifespan=lifespan)

    def get_db(request: Request) -> sqlite3.Connection:
        if db_factory is not None:
            return db_factory()
        return request.app.state.db

    def get_service(db: sqlite3.Connection = Depends(get_db)) -> TodoService:
        # `TodoService` is stateless w.r.t. connections; the per-request `db`
        # flows through to the repository on each call.
        return TodoService(TodoRepository(), UserTodoViewRepository())

    def enforce_subscription_gate(
        todo_id: int,
        user: AuthUser = Depends(verify_jwt),
        db: sqlite3.Connection = Depends(get_db),
        service: TodoService = Depends(get_service),
    ) -> None:
        """Reject requests for Todos the caller can't see.

        PATCH-specific closure dependency (the future DELETE path in T7
        uses a creator-only check, not a subscription check, so it
        won't share this). Mirrors `GET /todos/{id}`'s read gate: a
        `404` is raised for non-existent rows *and* for rows the caller
        can't see (no subscription, not the creator). The two failures
        collapse to the same envelope so the API doesn't leak
        existence (ADR-0003).

        Declared here as a closure (rather than at module scope) so it
        can resolve `get_db` / `get_service` / `verify_jwt` from the
        surrounding `create_app` body.
        """
        todo = service.get_for_user(db, todo_id=todo_id, user_id=user.sub)
        if todo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="todo not found",
            )

    # --- routes -----------------------------------------------------------

    @app.get("/todos")
    def list_todos(
        # Issue #8: comma-separated states. An absent or empty `?state`
        # falls back to the active-only default — `Query` cannot tell
        # those apart, so the helper normalizes both to `None`.
        state: str | None = Query(default=None),
        user: AuthUser = Depends(verify_jwt),
        db: sqlite3.Connection = Depends(get_db),
        service: TodoService = Depends(get_service),
    ) -> dict[str, list[dict[str, object]]]:
        states = _parse_state_filter(state)
        todos = service.list_for_user(db, user_id=user.sub, states=states)
        return {"todos": [_serialize_todo(t) for t in todos]}

    @app.post("/todos", status_code=status.HTTP_201_CREATED)
    def create_todo(
        body: CreateTodoBody = Body(...),
        user: AuthUser = Depends(verify_jwt),
        db: sqlite3.Connection = Depends(get_db),
        service: TodoService = Depends(get_service),
    ) -> dict[str, object]:
        try:
            todo = service.create(
                db,
                created_by=user.sub,
                input=CreateTodoInput(
                    title=body.title,
                    description=body.description or "",
                ),
            )
        except InvalidTitleError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="title must be a non-empty string",
            ) from None
        return _serialize_todo(todo)

    @app.get("/todos/{todo_id}")
    def get_todo(
        todo_id: int,
        user: AuthUser = Depends(verify_jwt),
        db: sqlite3.Connection = Depends(get_db),
        service: TodoService = Depends(get_service),
    ) -> dict[str, object]:
        # Subscription gate (ADR-0003): a user can read a todo only if they
        # are the creator (bypass) or they have a `user_todo_views` row.
        # "Doesn't exist" and "exists but no access" both collapse to 404
        # with the same body — see issue #4 acceptance criteria.
        todo = service.get_for_user(db, todo_id=todo_id, user_id=user.sub)
        if todo is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="todo not found",
            )
        return _serialize_todo(todo)

    @app.patch("/todos/{todo_id}")
    def patch_todo(
        todo_id: int,
        body: UpdateTodoBody = Body(...),
        # Subscription gate runs first: a non-subscriber (or non-creator)
        # gets `404` before we ever look at `If-Match`. Otherwise a
        # missing `If-Match` would accidentally confirm row existence
        # to a caller who has no read access.
        _: None = Depends(enforce_subscription_gate),
        if_match: str = Depends(require_if_match),
        user: AuthUser = Depends(verify_jwt),
        db: sqlite3.Connection = Depends(get_db),
        service: TodoService = Depends(get_service),
    ) -> dict[str, object]:
        # Build the domain input from the explicitly-set fields only.
        # `exclude_unset=True` lets `body.blocked_reason = None` mean
        # "clear the field" while omitting the key entirely means
        # "leave the stored value alone". `UpdateTodoInput` and the body
        # share the same four keys, so the spread maps 1:1; `UNSET` is
        # the dataclass default for any field the client didn't send.
        input = UpdateTodoInput(**body.model_dump(exclude_unset=True))

        try:
            todo = service.update(
                db,
                todo_id=todo_id,
                actor_id=user.sub,
                expected_updated_at=if_match,
                input=input,
            )
        except InvalidTitleError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="title must be a non-empty string",
            ) from None
        except (InvalidStateTransitionError, NotCreatorError):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="illegal state transition",
            ) from None
        except StalePreconditionError:
            # The atomic SQL UPDATE matched zero rows because the
            # client's `If-Match` no longer matches the row. The client
            # must re-read and retry; we don't merge or coerce.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="If-Match does not match the row's updated_at",
            ) from None

        return _serialize_todo(todo)

    @app.post(
        "/todos/{todo_id}/subscribe",
        status_code=status.HTTP_201_CREATED,
    )
    def subscribe_to_todo(
        todo_id: int,
        user: AuthUser = Depends(verify_jwt),
        db: sqlite3.Connection = Depends(get_db),
        service: TodoService = Depends(get_service),
    ) -> dict[str, object]:
        # `subscribe` is a public-to-authenticated-users operation: any
        # caller may join any existing todo. There's no per-todo ACL.
        # The service handles the not-found check and the duplicate-row
        # check; we just map the domain errors to status codes.
        try:
            service.subscribe(db, user_id=user.sub, todo_id=todo_id)
        except TodoNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="todo not found",
            ) from None
        except AlreadySubscribedError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="already subscribed",
            ) from None
        # Return just the `todo_id` — the caller already knows what
        # they subscribed to. A future ticker could surface the
        # assigned `position` so the client can build a reorder call
        # without re-reading; for now `GET /todos` is the source of
        # truth for that.
        return {"todo_id": todo_id}

    @app.delete("/todos/{todo_id}/subscribe")
    def unsubscribe_from_todo(
        todo_id: int,
        user: AuthUser = Depends(verify_jwt),
        db: sqlite3.Connection = Depends(get_db),
        service: TodoService = Depends(get_service),
    ) -> Response:
        # Idempotent by design: unsubscribing from a todo you never
        # joined (or that doesn't exist) is a `204`, not a `404`. The
        # resource is in the desired state after the call, and
        # forcing the client to check first is busywork. The domain
        # layer implements this; the HTTP layer just sets the status
        # code and returns an empty body.
        service.unsubscribe(db, user_id=user.sub, todo_id=todo_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def make_client(db: sqlite3.Connection) -> TestClient:
    """Build a TestClient sharing `db` across every request."""
    return TestClient(create_app(db_factory=lambda: db))


def _serialize_todo(todo: Todo) -> dict[str, object]:
    """Project a `domain.Todo` onto the API response shape.

    Kept module-private — the response shape is an HTTP concern, not a
    domain concern. Pydantic models could do this work, but a plain dict
    keeps the diff tight and matches the tracer bullet's existing style.
    """
    return {
        "id": todo.id,
        "title": todo.title,
        "description": todo.description,
        "state": todo.state.value,
        "created_by": todo.created_by,
        "created_at": todo.created_at,
        "updated_at": todo.updated_at,
        "completed_at": todo.completed_at,
        "deleted_at": todo.deleted_at,
        "blocked_reason": todo.blocked_reason,
    }


def _parse_state_filter(
    raw: str | None,
) -> frozenset[TodoState] | None:
    """Translate the `?state=...` query value into a domain-level filter.

    Returns `None` for the default-active behaviour (absent or empty
    parameter); an explicit set of `TodoState`s otherwise. Whitespace
    around each comma-separated token is trimmed so a caller passing
    `?state=pending, done` is treated identically to `?state=pending,done`.

    Unknown state names yield `422` with a body that names the offending
    value. The whole query fails — we don't partially apply the recognised
    names — because a partial filter would silently misrepresent what
    the caller asked for.
    """
    if raw is None or not raw.strip():
        return None
    states: set[TodoState] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            # Treat `?state=pending,,done` as if the empty slot wasn't
            # there; `?state=,` collapses to `None` above.
            continue
        try:
            states.add(TodoState(token))
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown state: {token}",
            ) from None
    if not states:
        # All tokens were empty after stripping; treat as default-active.
        return None
    return frozenset(states)