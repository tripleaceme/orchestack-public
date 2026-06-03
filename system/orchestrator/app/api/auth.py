"""Auth endpoints — login, logout, and current-user lookup.

Three endpoints back the M3.5 session-cookie flow:

    POST /api/auth/login    body: { username_or_email, password }
                            → 200 { user_id, username, full_name, email, roles[] }
                              + Set-Cookie: orchestack_session=<uuid>; HttpOnly;
                                            SameSite=Lax; Path=/

    POST /api/auth/logout   reads cookie; revokes the session row; returns 204
                            + Set-Cookie with Max-Age=0 to clear the cookie

    GET  /api/auth/me       reads cookie; returns the user's identity + roles
                            or 401 if the cookie is missing/expired/revoked

bcrypt verification runs under `asyncio.to_thread` so the CPU-bound work
(~100ms with bcrypt cost 12) doesn't block the event loop. The single-
host scale of OrcheStack means we don't need a verification pool; one
thread per request is fine.

Session row lifecycle:
    INSERT     on successful login
    UPDATE     last_seen_at on /api/auth/me (refresh the timestamp)
    UPDATE     revoked_at on logout
    DELETE     never — we keep history for the audit log

Cookie security:
    HttpOnly       — JS cannot read the cookie (XSS mitigation)
    SameSite=Lax   — Sent on same-site nav + top-level cross-site GET, NOT
                     on cross-site POST/XHR. Right tradeoff for an admin UI.
    Secure         — Only set when running behind HTTPS (controlled by the
                     SECURE_COOKIES env var; defaults to False for dev).
    Path=/         — All paths under the same host (dashboard + orchestrator
                     + auth all share the cookie).

The 12-hour TTL is set on the session row's `expires_at` column. The
cookie has matching `Max-Age` so the browser drops it automatically when
the server-side row expires.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import audit, db

log = logging.getLogger("orchestrator.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "orchestack_session"
SESSION_TTL = timedelta(hours=12)
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "false").lower() == "true"


# ---------- Helpers --------------------------------------------------------
async def _verify_password(plain: str, hashed: str) -> bool:
    """bcrypt verify off the event loop."""
    return await asyncio.to_thread(
        bcrypt.checkpw, plain.encode("utf-8"), hashed.encode("utf-8"),
    )


async def _load_user_by_login(username_or_email: str) -> dict | None:
    """Fetch the user row by username OR email (single query, OR'd)."""
    row = await db.fetchrow(
        """
        SELECT id, username, email, full_name, password_hash, is_active
        FROM platform.users
        WHERE username = $1 OR email = $1
        LIMIT 1
        """,
        username_or_email,
    )
    return dict(row) if row else None


async def _load_user_roles(user_id: int) -> list[str]:
    rows = await db.fetch(
        """
        SELECT r.name
        FROM platform.user_roles ur
        JOIN platform.roles r ON r.id = ur.role_id
        WHERE ur.user_id = $1
        ORDER BY r.name
        """,
        user_id,
    )
    return [r["name"] for r in rows]


def _set_session_cookie(response: Response, token: str, max_age_seconds: int) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=max_age_seconds, httponly=True,
        samesite="lax", secure=SECURE_COOKIES, path="/",
    )


async def resolve_session(request: Request) -> dict | None:
    """Look up the user behind the session cookie, or None.

    Public helper for other API modules (signup-followup, future
    permission checks). Returns the user dict + roles, or None for
    "no cookie / expired / revoked". Refreshes last_seen_at as a
    side effect on success.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = await db.fetchrow(
        """
        UPDATE platform.sessions
        SET last_seen_at = now()
        WHERE token = $1::uuid
          AND revoked_at IS NULL
          AND expires_at > now()
        RETURNING user_id
        """,
        token,
    )
    if row is None:
        return None
    user = await db.fetchrow(
        "SELECT id, username, email, full_name FROM platform.users WHERE id = $1 AND is_active = TRUE",
        row["user_id"],
    )
    if user is None:
        return None
    roles = await _load_user_roles(user["id"])
    return {
        "user_id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "full_name": user["full_name"],
        "roles": roles,
    }


# ---------- Schemas --------------------------------------------------------
class LoginRequest(BaseModel):
    username_or_email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


# ---------- Endpoints ------------------------------------------------------
@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    """Verify credentials + set the session cookie.

    Returns 401 for any failure (unknown user, wrong password, inactive
    account) — we deliberately don't differentiate, to avoid leaking
    which usernames exist.
    """
    user = await _load_user_by_login(req.username_or_email)

    # Constant-time-ish failure paths: bcrypt the password against a dummy
    # hash even when the user doesn't exist, so the response time doesn't
    # leak username existence. (A real attacker can still distinguish by
    # other means, but this removes the cheap timing oracle.)
    DUMMY_HASH = "$2b$12$" + "x" * 53  # well-formed bcrypt hash that won't verify
    target_hash = user["password_hash"] if user else DUMMY_HASH
    valid = await _verify_password(req.password, target_hash)

    if not user or not valid or not user["is_active"]:
        # Don't audit per-failed-login (would flood the table); only audit
        # successes here.
        raise HTTPException(401, "invalid credentials")

    # Insert session row. expires_at = now() + 12h.
    now = datetime.now(timezone.utc)
    expires_at = now + SESSION_TTL
    sess = await db.fetchrow(
        """
        INSERT INTO platform.sessions (user_id, expires_at, ip_address, user_agent)
        VALUES ($1, $2, $3, $4)
        RETURNING token::text AS token
        """,
        user["id"], expires_at,
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
    )
    token = sess["token"]

    # Update users.last_login_at (best-effort; if it fails, log doesn't matter).
    await db.execute(
        "UPDATE platform.users SET last_login_at = now() WHERE id = $1",
        user["id"],
    )

    await audit.write(
        "user_logged_in", user_id=user["id"],
        details={"username": user["username"]},
    )

    _set_session_cookie(response, token, int(SESSION_TTL.total_seconds()))

    roles = await _load_user_roles(user["id"])
    return {
        "user_id": user["id"],
        "username": user["username"],
        "full_name": user["full_name"],
        "email": user["email"],
        "roles": roles,
    }


@router.post("/logout")
async def logout(request: Request, response: Response) -> Response:
    """Revoke the current session + clear the cookie."""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        row = await db.fetchrow(
            """
            UPDATE platform.sessions
            SET revoked_at = now()
            WHERE token = $1::uuid AND revoked_at IS NULL
            RETURNING user_id
            """,
            token,
        )
        if row is not None:
            await audit.write("user_logged_out", user_id=row["user_id"])
    # Clear the cookie regardless of whether the session existed.
    response.delete_cookie(COOKIE_NAME, path="/")
    return Response(status_code=204)


@router.get("/me")
async def me(request: Request) -> dict:
    """Return the current user's identity + roles, or 401."""
    user = await resolve_session(request)
    if user is None:
        raise HTTPException(401, "not authenticated")
    return user
