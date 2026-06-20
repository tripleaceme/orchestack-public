"""Auth endpoints: session-cookie login, logout, and current-user lookup."""

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

# Real bcrypt hash used for unknown-username login attempts so verify timing
# matches the real-user path (timing-attack mitigation). Must be a valid bcrypt
# encoding — a hardcoded string triggers ValueError: Invalid salt in checkpw.
# Cost factor matches the real-user path so timings align.
_DUMMY_HASH = bcrypt.hashpw(
    b"\x00not-a-real-user-password\x00", bcrypt.gensalt(rounds=12),
).decode("utf-8")


# ---------- Helpers --------------------------------------------------------
def _checkpw(plain: bytes, hashed: bytes) -> bool:
    # Swallow ValueError/TypeError so a malformed password_hash row (corrupted /
    # manually edited) rejects credentials instead of 500-ing the login request.
    try:
        return bcrypt.checkpw(plain, hashed)
    except (ValueError, TypeError):
        return False


async def _verify_password(plain: str, hashed: str) -> bool:
    """bcrypt verify off the event loop. False on malformed hash, never raises."""
    return await asyncio.to_thread(
        _checkpw, plain.encode("utf-8"), hashed.encode("utf-8"),
    )


async def _load_user_by_login(username_or_email: str) -> dict | None:
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
    """Resolve the session cookie to a user dict + roles, or None; refreshes last_seen_at."""
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
    """Verify credentials + set the session cookie; 401 on any failure (no user-existence leak)."""
    user = await _load_user_by_login(req.username_or_email)

    # Verify against a dummy hash for unknown users so response time doesn't
    # leak username existence.
    target_hash = user["password_hash"] if user else _DUMMY_HASH
    valid = await _verify_password(req.password, target_hash)

    if not user or not valid or not user["is_active"]:
        # Deliberately no audit row on failure — would flood the table.
        raise HTTPException(401, "invalid credentials")

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
    # Clear cookie even if no session row existed (idempotent logout).
    response.delete_cookie(COOKIE_NAME, path="/")
    return Response(status_code=204)


@router.get("/me")
async def me(request: Request) -> dict:
    """Return the current user's identity + roles, or 401."""
    user = await resolve_session(request)
    if user is None:
        raise HTTPException(401, "not authenticated")
    return user
