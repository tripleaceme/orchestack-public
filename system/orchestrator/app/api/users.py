"""User-management endpoints — signup and (later) admin user listing.

For M3.5 only the signup endpoint matters: signup.html posts here, the
orchestrator bcrypts the password and inserts a row into platform.users,
then auto-assigns Admin if this is the first user.

The signup endpoint is intentionally unauthenticated — it has to be, to
bootstrap the first user. Phase 3.6 may add a "signup locked" toggle so
deployments past initial setup can disable further signups, but for M3
any unauthenticated POST creates an account.

After signup we DO NOT auto-login. The client (signup.html) is expected
to redirect to /app/login afterwards so the user signs in via the
normal flow — keeping signup and login as separate state transitions
makes the audit trail clearer (`user_created` vs `user_logged_in`
events both exist for the same user).
"""

from __future__ import annotations

import asyncio
import logging

import bcrypt
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import audit, db

# Pragmatic email regex — matches what the DB CHECK enforces (length > 0
# AND contains '@' past position 1). We deliberately don't pull in the
# `email-validator` package; the DB will catch any pathological inputs
# this misses, and we don't claim to validate every RFC 5322 case.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

log = logging.getLogger("orchestrator.users")

router = APIRouter(prefix="/api/users", tags=["users"])

BCRYPT_COST = 12


class SignupRequest(BaseModel):
    """Signup form payload. Validation here mirrors the schema CHECKs:
    username regex, email shape, full_name non-empty, password length."""
    username: str = Field(..., min_length=3, max_length=32,
                           pattern=r"^[a-zA-Z0-9_.\-]+$")
    email: str = Field(..., min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    full_name: str = Field(..., min_length=1)
    password: str = Field(..., min_length=12,
                           description="Minimum 12 characters; bcrypt-hashed before storage.")
    company_name: str | None = None


async def _hash_password(plain: str) -> str:
    """bcrypt hash off the event loop. ~100ms with cost 12."""
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(plain.encode("utf-8"),
                               bcrypt.gensalt(rounds=BCRYPT_COST)).decode("utf-8"),
    )


@router.post("", status_code=201)
async def signup(req: SignupRequest) -> dict:
    """Create a platform.users row. Returns the new user's id + username.

    First user gets auto-assigned the built-in Admin role; everyone else
    gets no roles initially (an Admin must grant them via the dashboard's
    admin UI in M3.4+).

    Duplicate username/email returns 409, NOT 400 — the schema's UNIQUE
    constraints make this a real conflict, not a validation error.
    """
    password_hash = await _hash_password(req.password)

    try:
        user_row = await db.fetchrow(
            """
            INSERT INTO platform.users
                (username, email, full_name, password_hash, company_name)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, username, email
            """,
            req.username, req.email.lower(), req.full_name,
            password_hash, req.company_name,
        )
    except UniqueViolationError as e:
        # Don't leak which field clashed — return a generic 409 with a
        # short hint that helps the form display a useful message.
        detail = "Username or email is already registered."
        if "users_username_key" in str(e):
            detail = "That username is taken."
        elif "users_email_key" in str(e):
            detail = "That email is already registered."
        raise HTTPException(409, detail)

    # Is this the first user? Auto-promote to Admin.
    count_row = await db.fetchrow("SELECT count(*) AS n FROM platform.users")
    is_first = count_row["n"] == 1
    if is_first:
        await db.execute(
            """
            INSERT INTO platform.user_roles (user_id, role_id)
            SELECT $1, id FROM platform.roles WHERE name = 'Admin'
            ON CONFLICT DO NOTHING
            """,
            user_row["id"],
        )

    await audit.write(
        "user_created", user_id=user_row["id"],
        details={"username": req.username, "is_first_user": is_first,
                  "company_name": req.company_name},
    )

    return {
        "user_id": user_row["id"],
        "username": user_row["username"],
        "email": user_row["email"],
        "is_first_user": is_first,
    }
