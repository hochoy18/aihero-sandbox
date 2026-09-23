"""JWT verification.

The service identifies users solely by the `sub` claim of a Bearer JWT. It
verifies signatures but does not issue tokens — that's out of scope per the
spec.

Authentication rules (issue #2):
  * No `Authorization` header        -> 401
  * Malformed `Authorization` value  -> 401
  * Token fails signature/exp checks -> 401
  * Token has no `sub` claim         -> 401
  * Otherwise                        -> AuthUser(sub=<sub>)

`make_jwt(sub=...)` is the test-time helper that mints a token signed with the
same secret. Production wires the secret through `TODO_JWT_SECRET`.

The default secret below is a deliberate placeholder so the module imports
cleanly outside any configured environment; the `TODO_JWT_SECRET` env var
must be set in any real deployment. Tightening this to a hard failure on a
missing env var is tracked for the deployment ticket.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, status


# Fallback used only when TODO_JWT_SECRET is unset. Padded to 32 bytes so
# PyJWT's HMAC key-length check stays quiet.
DEFAULT_JWT_SECRET = "test-secret-do-not-use-in-prod-0123456789"


def _secret() -> str:
    return os.environ.get("TODO_JWT_SECRET", DEFAULT_JWT_SECRET)


@dataclass(frozen=True)
class AuthUser:
    """The authenticated caller. Carries the JWT `sub` claim only."""

    sub: str


def make_jwt(sub: str) -> str:
    """Mint an HS256 JWT with `sub` as the only claim.

    Exposed so tests can produce valid tokens without an external IdP. The
    secret is read from the environment at call time, matching `verify_jwt`.
    """
    if not isinstance(sub, str) or not sub:
        raise ValueError("sub must be a non-empty string")
    return jwt.encode({"sub": sub}, _secret(), algorithm="HS256")


def verify_jwt(
    authorization: str | None = Header(default=None),
) -> AuthUser:
    """FastAPI dependency: parse the Bearer token and return the auth user.

    Every failure mode — missing header, wrong scheme, bad signature, missing
    claim — collapses to a 401. We deliberately do not distinguish them in the
    response body, matching the spec's "401 Unauthorized" envelope.
    """
    if not authorization:
        raise _unauthorized("missing authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _unauthorized("malformed authorization header")

    token = parts[1]
    try:
        payload = jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise _unauthorized("invalid token") from None

    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _unauthorized("missing sub claim")

    return AuthUser(sub=sub)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


__all__ = ["AuthUser", "DEFAULT_JWT_SECRET", "make_jwt", "verify_jwt"]