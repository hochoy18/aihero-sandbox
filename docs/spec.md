# Todo Backend Spec

## Problem Statement

The user wants a backend for a Todo application that supports shared todos with per-user customization. Today, `src/todo.py` contains only a minimal `TodoItem` class with a binary `done` flag — no auth, no users, no delete, no shared pool. There is no HTTP API, no persistence beyond in-memory Python objects, and no support for the multi-user behavior (subscriptions, ordering, concurrent edits) the user has designed.

## Solution

A Python library containing the domain logic, plus a thin HTTP service that exposes it. Todos live in a shared global pool; users explicitly subscribe to them and reorder their own view via a per-user `position`. State is global (anyone can transition), but only the creator can soft-delete. Optimistic concurrency on `updated_at` prevents silent overwrite. SQLite-backed persistence with JWT bearer auth.

## User Stories

1. As an authenticated user, I want to create a Todo, so that I can start tracking work in the shared pool.
2. As the creator of a Todo, I want to soft-delete it (transition to `deleted`), so that it disappears from everyone's view while keeping history.
3. As an authenticated user, I want to subscribe to a Todo, so that it appears in my personal view.
4. As a subscriber, I want to unsubscribe from a Todo, so that it stops appearing in my personal view.
5. As a subscriber, I want my view to be sorted by my custom `position`, so that the order reflects my priorities.
6. As a subscriber, I want to reorder my view in bulk, so that I can quickly rearrange my work.
7. As a subscriber, I want my default view to include only active Todos (state `pending` or `in_progress`), so that I see what is actionable today.
8. As a subscriber, I want to filter my view by `state`, so that I can look at completed or cancelled Todos when I need to.
9. As any authenticated user, I want to edit a Todo's title and description, so that I can keep its content accurate (collaborative, wikibot-style).
10. As any authenticated user, I want to transition a Todo's state through the 5-state lifecycle, so that progress is tracked.
11. As any authenticated user, I want to set or clear a Todo's `blocked_reason`, so that others understand why work is stuck.
12. As the system, I want to enforce the state machine, so that illegal transitions (e.g. `done` → `in_progress`) are rejected with `409 Conflict`.
13. As the system, I want to enforce optimistic concurrency on every write via `If-Match: <updated_at>`, so that concurrent edits don't silently overwrite each other.
14. As the system, I want to enforce creator-only soft-delete, so that arbitrary users cannot remove a Todo from the pool.
15. As the system, I want to enforce subscription-gated reads, so that a non-subscriber cannot see a Todo via direct `GET /todos/{id}` (returns `404`).
16. As the system, I want to enforce JWT bearer authentication on every request, so that the user identity is unambiguous.
17. As an unauthenticated requester, I want to be rejected with `401`, so that the API is protected by default.
18. As a subscriber, I want my Todo data to survive restarts, so that my work is durable.
19. As an integrator, I want the domain logic exposed as a Python library, so that I can build other transports on top.
20. As a deployer, I want SQLite for persistence, so that no external database is required.

## Implementation Decisions

### Architecture

- **Two layers**: a Python library (the canonical domain) and a thin HTTP service that exposes the library over REST.
- **Hexagonal/clean-lite**: the library depends on repository interfaces; SQLite is one concrete implementation. The HTTP layer depends only on the library.

### Modules

- **Domain layer (`TodoService`)**: owns state machine, validation rules, and business invariants. Pure Python; no I/O.
- **Persistence layer (`TodoRepository`, `UserTodoViewRepository`)**: SQLite-backed. Implements interfaces defined by the domain.
- **HTTP layer**: routes that translate HTTP requests into domain calls. Validates JWT, maps domain errors to HTTP status codes.

### Data model

Two tables:

- **`todos`**: `id`, `title`, `description`, `state`, `created_by`, `created_at`, `updated_at`, `completed_at`, `deleted_at`, `blocked_reason`.
- **`user_todo_views`**: `user_id`, `todo_id`, `position`, `subscribed_at`. Primary key: `(user_id, todo_id)`.

`created_by` records who created the Todo (used for creator-only delete permission); it is not a foreign key into a users table, because this service does not store user entities. Users are identified solely by the `sub` claim of their JWT.

### State machine

```
                  ┌──────────────┐
                  │   pending    │◄────────┐
                  └──────┬───────┘         │
            ┌────────────┼────────────┐    │
            ▼            ▼            ▼    │
       ┌─────────┐  ┌──────────┐  ┌───────┴───┐
       │  in_    │  │  done    │  │  pending  │  (in_progress ↔ pending free)
       │ progress│  │ (term)   │  └───────────┘
       └────┬────┘  └────┬─────┘
            │            │
            ├────────────┤
            ▼            ▼
       ┌──────────┐  ┌──────────┐
       │ cancelled│  │ deleted  │
       │ (term)   │  │ (term)   │
       └──────────┘  └──────────┘
```

- `pending` ↔ `in_progress`: freely reversible.
- `pending` / `in_progress` → `done` / `cancelled`: terminal except for delete.
- `done` / `cancelled` → `deleted`: allowed (creator-only).
- `pending` / `in_progress` → `deleted`: allowed (creator-only).
- `deleted` has no outgoing edges.
- `blocked` is **not** a state. It lives as the optional `blocked_reason: str | null` field on the Todo.

### HTTP API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/todos` | JWT | Create a Todo. Body: `{title, description?}`. Caller becomes `created_by`. |
| `GET` | `/todos` | JWT | List my subscribed Todos. Query: `?state=pending,in_progress` (default: active only). Sorted by my `position ASC`. |
| `GET` | `/todos/{id}` | JWT + subscription | Get one Todo. `404` if not subscribed. |
| `PATCH` | `/todos/{id}` | JWT + subscription + `If-Match: <updated_at>` | Update title / description / state / blocked_reason. Server validates state transition. |
| `DELETE` | `/todos/{id}` | JWT + creator | Soft-delete (transition to `deleted`). `404` if not creator. |
| `POST` | `/todos/{id}/subscribe` | JWT | Subscribe; creates a `user_todo_views` row at default `position`. |
| `DELETE` | `/todos/{id}/subscribe` | JWT | Unsubscribe; deletes the `user_todo_views` row. |
| `POST` | `/todos/reorder` | JWT | Body: `{order: [todo_id, todo_id, ...]}`. Rewrites my `position` for the listed Todos. |

### Error responses

- `401 Unauthorized` — missing or invalid JWT.
- `404 Not Found` — Todo doesn't exist, OR exists but caller has no subscription (we don't distinguish to avoid leaking existence). Also returned for non-creator attempting to delete.
- `409 Conflict` — illegal state transition, or `If-Match` mismatch.
- `422 Unprocessable Entity` — request body validation (missing title, malformed list, etc.).

### Concurrency

- Every `PATCH` and `DELETE` requires `If-Match: <updated_at>`.
- Server checks the `updated_at` matches the row; if not, returns `409`.
- On success, server refreshes `updated_at` to `now()` (UTC).
- All timestamps are server-set only; clients cannot override them.

### Defaults

- `position` for a new subscription: `(current max position for this user) + 1`.
- Default `GET /todos` excludes `state=deleted`, `state=done`, and `state=cancelled` (active-only). `?state=...` overrides.
- No pagination; the full subscribed list is returned.

### Permissions summary

| Action | Anyone | Subscriber | Creator |
|---|---|---|---|
| `POST /todos` | ✓ (becomes creator) | ✓ | ✓ |
| `GET /todos/{id}` |   | ✓ | ✓ (even if not subscribed) |
| `PATCH` title / description / blocked_reason |   | ✓ | ✓ |
| `PATCH` state |   | ✓ | ✓ |
| `DELETE /todos/{id}` (→ `deleted`) |   |   | ✓ |
| Subscribe / unsubscribe |   | ✓ | ✓ |
| Reorder own view |   | ✓ | ✓ |

### Persistence

- SQLite via Python's stdlib `sqlite3` (or a thin async wrapper if the HTTP layer is async).
- Schema creation on startup (idempotent `CREATE TABLE IF NOT EXISTS` for MVP; a migration tool is out of scope).
- UTC timestamps throughout.

## Testing Decisions

Test external behavior — domain public methods and HTTP endpoints — not internal implementation.

- **Unit tests on `TodoService`**: state machine transitions (legal/illegal), permissions (creator-only delete), `blocked_reason` semantics, `position` assignment on subscribe.
- **Integration tests on the HTTP layer**: end-to-end CRUD, subscribe flow, reorder, optimistic concurrency (`If-Match` mismatch returns `409`), JWT rejection (`401`), non-subscriber `404`, creator-only delete.
- **Test isolation**: each test uses a fresh in-memory SQLite database; teardown drops tables.
- **Test framework**: pytest. No existing test infrastructure in this repo (greenfield).
- **Highest seam**: HTTP tests cover most flows; reserve unit tests for state-machine edge cases and permission rules that are awkward to drive via HTTP.

## Out of Scope

- Hard delete (the row stays in storage after soft delete).
- Restoring a `deleted` Todo.
- Bulk operations other than reorder.
- Title uniqueness, deduplication, or search across titles.
- `description` rich text / markdown.
- `due_date`, `priority`, or any other field beyond what's specified.
- Audit log of who did what.
- Tags, labels, or categories (we rejected tags for MVP; `blocked_reason` is the only tag-like field).
- Push notifications or real-time updates (WebSockets, SSE).
- Sharing via invite links; workspace / team concept; admin roles.
- TTL-based auto-cleanup of `deleted` Todos.
- Multi-device client-side merge UI (server enforces via `409`; the UI is the consumer's job).
- User entity storage (no name, email, profile) — the service identifies users only by JWT `sub`.
- Token issuance / auth provider integration — JWT verification only.
- Pagination (MVP returns the full subscribed list).
- Internationalization.
- Backups / disaster recovery beyond SQLite file durability.

## Further Notes

- The Python library is the canonical interface; HTTP is one transport. Any future transport (CLI, gRPC) reuses the library.
- `src/todo.py` is a vestigial stub and will be replaced by the new modules. The original `TodoItem` is not the production model.
- The 5-state machine is documented in `CONTEXT.md` and `docs/adr/0001-todo-lifecycle.md`.
- The shared-pool + subscription model is documented in `docs/adr/0003-shared-pool-with-subscriptions.md` (which supersedes the original `docs/adr/0002-per-user-private-with-jwt.md`).
- `blocked_reason` is global (not per-user); see `CONTEXT.md`.
- Creator permission is role-based, not view-based: a creator who has unsubscribed can still `DELETE` the Todo.
- `description` is plain text; length cap is a validation concern (suggested cap: 10 KB), enforced at the HTTP layer.
- `title` length cap: suggested 200 chars.
