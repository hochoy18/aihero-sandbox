# Todo lifecycle is a 5-state machine; `deleted` is terminal

A Todo moves through 5 states: `pending`, `in_progress`, `done`, `cancelled`, `deleted`. `done` and `cancelled` are terminal — neither can transition to any other state except `deleted`. `deleted` is reachable from any state and is itself terminal (no restore). `in_progress` ↔ `pending` is freely reversible.

`blocked` is **not** a state. It lives as the optional `blocked_reason: str | null` field on the todo, kept orthogonal to the lifecycle.

## Considered options

- **Binary `done` flag** (the original `src/todo.py` shape) — rejected: loses the "started but not finished" distinction, and forces `done`/`cancelled` to collapse onto one flag.
- **Wider state machine with `blocked` as a state** — rejected: blends orthogonal "why blocked" text into lifecycle transitions; you'd need a transition every time you fill in or clear the reason.
- **Soft delete as a separate `deleted_at` column** — rejected: would lose the "deleted is a state like any other" symmetry, and complicate transitions (every state would need to remember whether it's also deleted).
- **`deleted` as a state but restorable** — rejected: complicates the state machine with a transition out of `deleted`; the "undo" use case is better served by `cancelled` + create-new.

## Consequences

- `done`/`cancelled` are true tombstones for normal use; the only way out is `delete`. If a user wants to "reopen" a `done` todo, they have to delete it and re-create.
- Every state transition must be explicitly allowed by the server; the client can't `PATCH` its way into an illegal state. Invalid transitions return `409 Conflict`.
- `blocked_reason` is a free-text field; the system never reasons about its content.
