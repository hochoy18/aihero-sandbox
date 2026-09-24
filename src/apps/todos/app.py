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

Tracers left by issue #2:
  * `GET  /todos`              — empty list; subscription list lands in #8.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from .auth import AuthUser, verify_jwt
from .db import make_db
from .domain import CreateTodoInput, InvalidTitleError, Todo, TodoService
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

    # --- routes -----------------------------------------------------------

    @app.get("/todos")
    def list_todos(user: AuthUser = Depends(verify_jwt)) -> dict[str, list[dict[str, object]]]:
        # Tracer bullet: subscription model lands in #8.
        _ = user
        return {"todos": []}

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