"""User-management endpoints: signup, self-service profile, service permissions."""

from __future__ import annotations

import asyncio
import logging

import bcrypt
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db
from .auth import resolve_session

# Pragmatic email regex — matches what the DB CHECK enforces (length > 0
# AND contains '@' past position 1). We deliberately don't pull in the
# `email-validator` package; the DB will catch any pathological inputs
# this misses, and we don't claim to validate every RFC 5322 case.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

log = logging.getLogger("orchestrator.users")

router = APIRouter(prefix="/api/users", tags=["users"])

BCRYPT_COST = 12


class SignupRequest(BaseModel):
    """Signup form payload; validation mirrors the schema CHECKs."""
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
    """Create the first platform user; returns 403 once any real user exists."""
    # Filter out the seeded non-loginable system row (id=1, username='system')
    # which exists so FK constraints on audit/sessions are satisfied before
    # the first real user signs up — see postgres-init/20-seed-default-user.sql.
    # Filter by username, not id, so this stays robust if id=1 ever shifts.
    pre_count = await db.fetchrow(
        "SELECT count(*) AS n FROM platform.users WHERE username != 'system'"
    )
    if pre_count and pre_count["n"] > 0:
        raise HTTPException(
            403,
            "Self-signup is disabled. Ask an administrator to invite you via "
            "the dashboard Users page.",
        )

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
        detail = "Username or email is already registered."
        if "users_username_key" in str(e):
            detail = "That username is taken."
        elif "users_email_key" in str(e):
            detail = "That email is already registered."
        raise HTTPException(409, detail)

    # Same 'system' filter as the gate above — seeded row isn't a real user.
    count_row = await db.fetchrow(
        "SELECT count(*) AS n FROM platform.users WHERE username != 'system'"
    )
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


# Self-service profile. Username is intentionally read-only here — username
# changes are an Admin operation because they affect audit logs and Git authorship.

class ProfileUpdateRequest(BaseModel):
    """Partial update — any field omitted is left untouched."""
    full_name:    str | None = Field(default=None, min_length=1, max_length=120)
    email:        str | None = Field(default=None, min_length=3, max_length=320,
                                       pattern=EMAIL_PATTERN)
    company_name: str | None = Field(default=None, max_length=120)
    # Require current_password on password change to defend against session
    # hijack leading to credential takeover.
    current_password: str | None = None
    new_password:     str | None = Field(default=None, min_length=12)


@router.get("/me")
async def get_my_profile(request: Request) -> dict:
    """The signed-in user's own profile, including roles."""
    user = await resolve_session(request)
    if user is None:
        raise HTTPException(401, "not authenticated")
    row = await db.fetchrow(
        """
        SELECT id, username, email, full_name, company_name,
                is_active, created_at, last_login_at
        FROM platform.users WHERE id = $1
        """,
        user["user_id"],
    )
    if row is None:
        raise HTTPException(404, "user not found")
    return {
        "id":            row["id"],
        "username":      row["username"],
        "email":         row["email"],
        "full_name":     row["full_name"],
        "company_name":  row["company_name"],
        "is_active":     row["is_active"],
        "created_at":    row["created_at"].isoformat() if row["created_at"] else None,
        "last_login_at": row["last_login_at"].isoformat() if row["last_login_at"] else None,
        "roles":         user.get("roles", []),
    }


@router.patch("/me")
async def update_my_profile(req: ProfileUpdateRequest, request: Request) -> dict:
    """Partial update to the signed-in user's own profile."""
    user = await resolve_session(request)
    if user is None:
        raise HTTPException(401, "not authenticated")
    user_id = user["user_id"]

    if req.new_password and not req.current_password:
        raise HTTPException(
            400,
            "current_password is required when changing your password.",
        )

    new_hash = None
    if req.new_password:
        # bcrypt check runs via asyncio.to_thread to keep the event loop free.
        row = await db.fetchrow(
            "SELECT password_hash FROM platform.users WHERE id = $1",
            user_id,
        )
        existing = row["password_hash"].encode("utf-8") if row else b""
        ok = await asyncio.to_thread(
            lambda: bcrypt.checkpw(
                (req.current_password or "").encode("utf-8"), existing
            ) if existing else False
        )
        if not ok:
            raise HTTPException(403, "Current password is incorrect.")
        new_hash = await _hash_password(req.new_password)

    # Pydantic v2: model_fields_set distinguishes "omitted" from "explicit None".
    sets:   list[str] = []
    values: list = []
    next_idx = 1

    if "full_name" in req.model_fields_set and req.full_name is not None:
        sets.append(f"full_name = ${next_idx}");    values.append(req.full_name);    next_idx += 1
    if "email" in req.model_fields_set and req.email is not None:
        sets.append(f"email = ${next_idx}");        values.append(req.email.lower()); next_idx += 1
    if "company_name" in req.model_fields_set:
        sets.append(f"company_name = ${next_idx}"); values.append(req.company_name); next_idx += 1
    if new_hash is not None:
        sets.append(f"password_hash = ${next_idx}"); values.append(new_hash);        next_idx += 1

    if not sets:
        return await get_my_profile(request)

    values.append(user_id)
    sql = (
        f"UPDATE platform.users SET {', '.join(sets)} "
        f"WHERE id = ${next_idx} "
        f"RETURNING id, username, email, full_name, company_name"
    )
    try:
        updated = await db.fetchrow(sql, *values)
    except UniqueViolationError as e:
        detail = "That email is already registered." if "users_email_key" in str(e) \
                  else "That value is already taken."
        raise HTTPException(409, detail)

    # Log keys, not values — never leak PII or password material into audit.
    await audit.write(
        "profile_updated", user_id=user_id,
        details={"updated_keys": [s.split(" = ")[0] for s in sets]},
    )
    return {
        "id":          updated["id"],
        "username":    updated["username"],
        "email":       updated["email"],
        "full_name":   updated["full_name"],
        "company_name": updated["company_name"],
    }


@router.get("/me/services")
async def list_my_service_permissions(request: Request) -> dict:
    """Return {allowed_services: [...]} for the signed-in user."""
    user = await resolve_session(request)
    if user is None:
        raise HTTPException(401, "not authenticated")

    from .. import config as _cfg
    if "Admin" in user.get("roles", []):
        return {"allowed_services": list(_cfg.SERVICE_CATALOGUE.keys())}

    rows = await db.fetch(
        """
        SELECT DISTINCT rp.service_name
        FROM platform.role_permissions rp
        JOIN platform.user_roles ur ON ur.role_id = rp.role_id
        WHERE ur.user_id = $1
          AND (rp.can_use OR rp.can_start)
        """,
        user["user_id"],
    )
    # NULL service_name in role_permissions is the operator wildcard grant.
    names: set[str] = set()
    saw_wildcard = False
    for r in rows:
        if r["service_name"] is None:
            saw_wildcard = True
            break
        names.add(r["service_name"])
    if saw_wildcard:
        names = set(_cfg.SERVICE_CATALOGUE.keys())
    return {"allowed_services": sorted(names)}
