# Shared todo pool with per-user subscriptions and views

**Supersedes ADR-0002.**

Todos live in a shared global pool. There is no per-todo owner. To see a todo, a user must explicitly subscribe to it; the subscription creates a row in `user_todo_views(user_id, todo_id, position, subscribed_at)`. Each user's view shows only their subscribed todos, ordered by that user's `position`. Any authenticated user can edit a todo's title or description (wikibot-style, with optimistic concurrency on `updated_at`). Any user can transition state among `pending` / `in_progress` / `done` / `cancelled`. Only the **creator** — the user that originally created the todo — can transition it to `deleted`.

## Considered options

- **Per-user private todos** (superseded ADR-0002) — rejected mid-design: user clarified they wanted a shared pool with per-user ordering.
- **Shared pool with auto-view** (every user sees every todo automatically) — rejected: too noisy; users want explicit curation.
- **Shared pool with read-only content edits** — rejected: too restrictive; collaboration is the point.
- **Any user can globally delete** — rejected: too dangerous; creator-only is the safer default.
- **Per-user `blocked_reason`** — rejected in favor of global: the block reason reads as a property of the todo, not the user's view of it.

## Consequences

- A user without a subscription cannot see a todo, even via direct `GET /todos/{id}` — the API returns `404` (we don't distinguish "doesn't exist" from "exists but I'm not subscribed").
- Optimistic concurrency (`If-Match: <updated_at>`) applies to all writes — title, description, and state. Missing or stale `If-Match` returns `409`.
- `user_todo_views.position` is a per-user integer; users reorder their own view via `POST /todos/reorder`. Subscriptions created later default to `(current max position) + 1`.
- Subscriptions to `deleted` todos are not auto-removed; they simply don't show in default views. The user can `DELETE /user_todo_views/{todo_id}` to unsubscribe.
- The creator's edit rights on their own todo's content are no different from any other user's (anyone can edit). Creator-only is just the global delete.
