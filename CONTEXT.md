# Todo Backend

A Todo application's backend. Todos live in a shared global pool; users subscribe to them and curate their own per-user views.

## Language

**Todo**:
A task item in the shared global pool. Goes through a 5-state lifecycle. Has a creator. Anyone with a subscription can see it; anyone authenticated can edit its content or transition its state.
_Avoid_: Task, item, note

**User**:
An account. Does not own todos; subscribes to them. Many users can subscribe to the same todo.
_Avoid_: Account, member, owner

**Creator**:
The user that originally created a todo. Has a special permission: only the creator can transition the todo to `deleted`. Anyone else can edit content or transition state among the non-deleted states.
_Avoid_: Author, owner

**State**:
The position of a Todo in its lifecycle. One of: `pending`, `in_progress`, `done`, `cancelled`, `deleted`.

**pending**:
Default state on creation. The todo has not yet been started.

**in_progress**:
The todo is actively being worked on. Reachable from `pending`; freely revertible back to `pending`.

**done**:
The todo's work is complete. Terminal: cannot transition to any other state except `deleted`.

**cancelled**:
Decision not to do the todo. Terminal: cannot transition to any other state except `deleted`.

**deleted**:
The todo has been soft-deleted by its creator. Reachable from `pending` / `in_progress` / `done` / `cancelled`. The row stays in storage; nobody can restore it.
_Avoid_: archived, trashed

**Subscription**:
A user's act of adding a todo to their own view. Stored as a row in `user_todo_views(user_id, todo_id, position, subscribed_at)`. Required to see a todo in `GET /todos`.
_Avoid_: Follow, save, star, bookmark

**Position**:
The per-user ordering of a todo within that user's view. Integer. Lives in `user_todo_views`.
_Avoid_: rank, order

**blocked_reason**:
Free-text explanation of why the todo is blocked. Global: any user can edit, all users see the same value. `null` when not blocked.
_Avoid_: block_note, pause_reason
