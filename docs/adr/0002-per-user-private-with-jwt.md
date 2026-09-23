# Per-user private todos with JWT bearer auth

**Status: superseded by ADR-0003.**

Each Todo has exactly one `owner_id`. The current owner is identified by the `sub` claim of a JWT bearer token presented in the `Authorization` header. Every operation — read, write, state transition, delete — verifies `jwt.sub == todo.owner_id`. Other users cannot see, list, or modify another user's todos.

## Considered options

- **Shared workspaces** — rejected for MVP: out of scope; "every user has their own list" is the explicit first cut.
- **Per-user API keys** — rejected: revocation requires table updates; JWT is stateless and standard.
- **Unauthenticated, user_id in URL/body** — rejected: anyone could act as any user.
- **Session cookies** — rejected: ties the backend to browser-shaped clients; JWT bearer works equally well for web, mobile, and CLI.

## Consequences

- The HTTP layer is a thin wrapper over the Python library; ownership checks live in the library so they're enforced regardless of transport.
- A non-owner querying another user's todo gets `404 Not Found` (we do not distinguish "doesn't exist" from "exists but isn't yours", to avoid leaking existence).
- JWT verification needs a signing key (HS256 secret or RS256 public key) configured at deploy time; rotation is out of scope for MVP.
