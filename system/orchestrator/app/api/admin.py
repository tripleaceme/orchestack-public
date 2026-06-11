"""Admin endpoints — users, roles, role-permissions.

Every endpoint in this module requires the caller to hold the built-in
'Admin' role (verified via `_require_admin` against the session cookie
the dashboard forwards). The system seed user (Admin role) is the only
account that can hit these by default; subsequent admins can be created
by an existing admin via POST /api/admin/users/invite.

Endpoints overview:

  Users:
    GET    /api/admin/users                  → list every user with their roles
    POST   /api/admin/users/invite           → create a user with a starter password
    POST   /api/admin/users/{id}/disable     → soft-disable (is_active = false)
    POST   /api/admin/users/{id}/enable      → re-activate

  User-role assignments:
    POST   /api/admin/user-roles             → grant a role to a user
    DELETE /api/admin/user-roles/{u}/{r}     → revoke a role from a user

  Roles:
    GET    /api/admin/roles                  → list every role (built-in + custom)
    POST   /api/admin/roles                  → create a custom role
    DELETE /api/admin/roles/{id}             → delete a custom role (not system roles)

  Role-permissions (per role × per service):
    GET    /api/admin/role-permissions       → list every grant
    POST   /api/admin/role-permissions       → grant a permission set
    PUT    /api/admin/role-permissions/{id}  → update a grant
    DELETE /api/admin/role-permissions/{id}  → revoke a grant

Permission model
----------------
The 4 platform-level permissions on platform.role_permissions are:

    can_start         — start a service via the dashboard
    can_use           — open a session against the service (which the
                        orchestrator may auto-start)
    can_force_stop    — stop a service even when OTHER users have active
                        sessions (always audited separately)
    can_edit_config   — edit credentials on /app/credentials

These are PLATFORM permissions — what the OrcheStack user can do to the
service through OrcheStack's own surface. They are NOT the tool's own
internal permissions (e.g. Metabase dashboard editing, Airflow DAG triggering).
M4+ may layer tool-internal permission propagation on top via per-tool
API calls when a user opens a session.

service_name = '*' is a wildcard meaning "applies to every service". Per-
service rows override the wildcard for that specific service. The query
that resolves a user's effective permission for a given service is:

    SELECT *
    FROM   platform.role_permissions
    WHERE  role_id IN (user's role ids)
      AND  (service_name = ? OR service_name = '*')
    ORDER  BY (service_name = '*')   -- specific row wins over wildcard
    LIMIT  1

Audit log
---------
Every state-changing endpoint here writes an audit row. The KEY is logged
(role name, service name, permission flag) but secret values (passwords,
emails) are not.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string

import bcrypt
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit, db
from .auth import resolve_session

log = logging.getLogger("orchestrator.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

BCRYPT_COST = 12

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


# ===========================================================================
#  Admin auth dependency
# ===========================================================================
async def _require_admin(request: Request) -> dict:
    """Resolve the session cookie + require the Admin role. 401 / 403 otherwise."""
    user = await resolve_session(request)
    if user is None:
        raise HTTPException(401, "not authenticated")
    if "Admin" not in user.get("roles", []):
        raise HTTPException(403, "Admin role required")
    return user


# ===========================================================================
#  Users
# ===========================================================================
@router.get("/users")
async def list_users(admin: dict = Depends(_require_admin)) -> dict:
    """Every user + their roles."""
    rows = await db.fetch(
        """
        SELECT
          u.id, u.username, u.email, u.full_name, u.company_name,
          u.is_active, u.last_login_at, u.created_at,
          COALESCE(array_agg(r.name) FILTER (WHERE r.name IS NOT NULL), '{}') AS role_names
        FROM platform.users u
        LEFT JOIN platform.user_roles ur ON ur.user_id = u.id
        LEFT JOIN platform.roles r       ON r.id       = ur.role_id
        WHERE u.username != 'system'
        GROUP BY u.id
        ORDER BY u.created_at ASC
        """
    )
    return {
        "users": [
            {
                "id":             r["id"],
                "username":       r["username"],
                "email":          r["email"],
                "full_name":      r["full_name"],
                "company_name":   r["company_name"],
                "is_active":      r["is_active"],
                "last_login_at":  r["last_login_at"].isoformat() if r["last_login_at"] else None,
                "created_at":     r["created_at"].isoformat() if r["created_at"] else None,
                "roles":          list(r["role_names"]),
            }
            for r in rows
        ]
    }


class InviteRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32,
                           pattern=r"^[a-zA-Z0-9_.\-]+$")
    email: str = Field(..., min_length=3, max_length=320, pattern=EMAIL_PATTERN)
    full_name: str = Field(..., min_length=1)
    role_names: list[str] = Field(default_factory=list,
                                    description="Role names to grant on creation, e.g. ['Engineer']")


async def _hash(plain: str) -> str:
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(plain.encode("utf-8"),
                               bcrypt.gensalt(rounds=BCRYPT_COST)).decode("utf-8"),
    )


def _gen_starter_password(length: int = 16) -> str:
    """Cryptographically random starter password. Operator gives it to the
    invitee out-of-band; the invitee changes it on first login."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.post("/users/invite", status_code=201)
async def invite_user(req: InviteRequest,
                       admin: dict = Depends(_require_admin)) -> dict:
    """Create a user with a system-generated starter password.

    Returns the starter password ONCE — admin should hand it to the
    invitee out-of-band. We do NOT email it; that requires SMTP config
    which is out of scope for M3.6.
    """
    starter_pw = _gen_starter_password()
    password_hash = await _hash(starter_pw)

    try:
        row = await db.fetchrow(
            """
            INSERT INTO platform.users
                (username, email, full_name, password_hash)
            VALUES ($1, $2, $3, $4)
            RETURNING id, username, email
            """,
            req.username, req.email.lower(), req.full_name, password_hash,
        )
    except UniqueViolationError as e:
        detail = "Username or email is already registered."
        if "users_username_key" in str(e):
            detail = "That username is taken."
        elif "users_email_key" in str(e):
            detail = "That email is already registered."
        raise HTTPException(409, detail)

    # Grant requested roles, if any. Skip silently for unknown role names —
    # the audit row records which were actually granted.
    granted: list[str] = []
    if req.role_names:
        result = await db.fetch(
            """
            INSERT INTO platform.user_roles (user_id, role_id, granted_by_user_id)
            SELECT $1, r.id, $2
            FROM platform.roles r
            WHERE r.name = ANY($3::text[])
            ON CONFLICT DO NOTHING
            RETURNING role_id
            """,
            row["id"], admin["user_id"], req.role_names,
        )
        granted_role_ids = [r["role_id"] for r in result]
        if granted_role_ids:
            names = await db.fetch(
                "SELECT name FROM platform.roles WHERE id = ANY($1::bigint[])",
                granted_role_ids,
            )
            granted = [r["name"] for r in names]

    await audit.write(
        "user_invited", user_id=admin["user_id"],
        details={"new_user_id": row["id"], "username": req.username,
                  "roles_granted": granted},
    )
    return {
        "user_id":          row["id"],
        "username":         row["username"],
        "email":            row["email"],
        "starter_password": starter_pw,
        "roles_granted":    granted,
    }


@router.post("/users/{user_id}/disable")
async def disable_user(user_id: int, admin: dict = Depends(_require_admin)) -> dict:
    if user_id == admin["user_id"]:
        raise HTTPException(409, "You can't disable your own account.")
    res = await db.execute(
        "UPDATE platform.users SET is_active = FALSE WHERE id = $1 AND is_active = TRUE",
        user_id,
    )
    await audit.write("user_disabled", user_id=admin["user_id"],
                       details={"target_user_id": user_id})
    return {"ok": True, "changed": "1" in res}


@router.post("/users/{user_id}/enable")
async def enable_user(user_id: int, admin: dict = Depends(_require_admin)) -> dict:
    res = await db.execute(
        "UPDATE platform.users SET is_active = TRUE WHERE id = $1 AND is_active = FALSE",
        user_id,
    )
    await audit.write("user_enabled", user_id=admin["user_id"],
                       details={"target_user_id": user_id})
    return {"ok": True, "changed": "1" in res}


# ===========================================================================
#  User-role assignments
# ===========================================================================
class UserRoleRequest(BaseModel):
    user_id: int
    role_id: int


@router.post("/user-roles", status_code=201)
async def grant_user_role(req: UserRoleRequest,
                           admin: dict = Depends(_require_admin)) -> dict:
    await db.execute(
        """
        INSERT INTO platform.user_roles (user_id, role_id, granted_by_user_id)
        VALUES ($1, $2, $3)
        ON CONFLICT DO NOTHING
        """,
        req.user_id, req.role_id, admin["user_id"],
    )
    await audit.write(
        "user_role_granted", user_id=admin["user_id"],
        details={"target_user_id": req.user_id, "role_id": req.role_id},
    )
    return {"ok": True}


@router.delete("/user-roles/{user_id}/{role_id}")
async def revoke_user_role(user_id: int, role_id: int,
                             admin: dict = Depends(_require_admin)) -> dict:
    # Defensive: prevent admin from revoking their own Admin role and
    # locking themselves out.
    if user_id == admin["user_id"]:
        row = await db.fetchrow(
            "SELECT name FROM platform.roles WHERE id = $1", role_id,
        )
        if row and row["name"] == "Admin":
            raise HTTPException(409, "You can't revoke your own Admin role.")
    res = await db.execute(
        "DELETE FROM platform.user_roles WHERE user_id = $1 AND role_id = $2",
        user_id, role_id,
    )
    await audit.write(
        "user_role_revoked", user_id=admin["user_id"],
        details={"target_user_id": user_id, "role_id": role_id},
    )
    return {"ok": True, "changed": "1" in res}


# ===========================================================================
#  Roles
# ===========================================================================
@router.get("/roles")
async def list_roles(admin: dict = Depends(_require_admin)) -> dict:
    rows = await db.fetch(
        """
        SELECT id, name, description, is_system, created_at
        FROM platform.roles
        ORDER BY is_system DESC, name ASC
        """
    )
    return {
        "roles": [
            {
                "id":          r["id"],
                "name":        r["name"],
                "description": r["description"],
                "is_system":   r["is_system"],
                "created_at":  r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


class CreateRoleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str | None = None


@router.post("/roles", status_code=201)
async def create_role(req: CreateRoleRequest,
                       admin: dict = Depends(_require_admin)) -> dict:
    try:
        row = await db.fetchrow(
            """
            INSERT INTO platform.roles (name, description, is_system)
            VALUES ($1, $2, FALSE)
            RETURNING id, name
            """,
            req.name, req.description,
        )
    except UniqueViolationError:
        raise HTTPException(409, f"A role named {req.name!r} already exists.")
    await audit.write(
        "role_created", user_id=admin["user_id"],
        details={"role_id": row["id"], "role_name": row["name"]},
    )
    return {"role_id": row["id"], "name": row["name"]}


@router.delete("/roles/{role_id}")
async def delete_role(role_id: int, admin: dict = Depends(_require_admin)) -> dict:
    row = await db.fetchrow(
        "SELECT name, is_system FROM platform.roles WHERE id = $1", role_id,
    )
    if not row:
        raise HTTPException(404, "Role not found.")
    if row["is_system"]:
        raise HTTPException(409, "Built-in roles cannot be deleted.")
    await db.execute("DELETE FROM platform.roles WHERE id = $1", role_id)
    await audit.write(
        "role_deleted", user_id=admin["user_id"],
        details={"role_id": role_id, "role_name": row["name"]},
    )
    return {"ok": True}


# ===========================================================================
#  Role-permissions
# ===========================================================================
class PermissionGrant(BaseModel):
    role_id: int
    service_name: str = Field(..., min_length=1,
                                description="Catalogue service name or '*' for all")
    can_start:      bool = False
    can_use:        bool = False
    can_force_stop: bool = False
    can_edit_config: bool = False


@router.get("/role-permissions")
async def list_role_permissions(
    role_id: int | None = None,
    admin: dict = Depends(_require_admin),
) -> dict:
    if role_id is not None:
        rows = await db.fetch(
            """
            SELECT rp.id, rp.role_id, r.name AS role_name, rp.service_name,
                   rp.can_start, rp.can_use, rp.can_force_stop, rp.can_edit_config,
                   rp.created_at, rp.updated_at
            FROM platform.role_permissions rp
            JOIN platform.roles r ON r.id = rp.role_id
            WHERE rp.role_id = $1
            ORDER BY rp.service_name
            """,
            role_id,
        )
    else:
        rows = await db.fetch(
            """
            SELECT rp.id, rp.role_id, r.name AS role_name, rp.service_name,
                   rp.can_start, rp.can_use, rp.can_force_stop, rp.can_edit_config,
                   rp.created_at, rp.updated_at
            FROM platform.role_permissions rp
            JOIN platform.roles r ON r.id = rp.role_id
            ORDER BY r.name, rp.service_name
            """
        )
    return {
        "permissions": [
            {
                "id":              r["id"],
                "role_id":         r["role_id"],
                "role_name":       r["role_name"],
                "service_name":    r["service_name"],
                "can_start":       r["can_start"],
                "can_use":         r["can_use"],
                "can_force_stop":  r["can_force_stop"],
                "can_edit_config": r["can_edit_config"],
                "created_at":      r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at":      r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]
    }


@router.post("/role-permissions", status_code=201)
async def grant_permission(req: PermissionGrant,
                             admin: dict = Depends(_require_admin)) -> dict:
    """Grant (or replace) the per-service permission set for a role.

    Upserts on (role_id, service_name) — if a grant exists, it's
    overwritten. This is the operation behind the dashboard's "Add
    service permission" form.
    """
    row = await db.fetchrow(
        """
        INSERT INTO platform.role_permissions
            (role_id, service_name, can_start, can_use, can_force_stop, can_edit_config)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (role_id, service_name) DO UPDATE SET
            can_start       = EXCLUDED.can_start,
            can_use         = EXCLUDED.can_use,
            can_force_stop  = EXCLUDED.can_force_stop,
            can_edit_config = EXCLUDED.can_edit_config,
            updated_at      = now()
        RETURNING id
        """,
        req.role_id, req.service_name,
        req.can_start, req.can_use, req.can_force_stop, req.can_edit_config,
    )
    await audit.write(
        "role_permission_granted", user_id=admin["user_id"],
        details={
            "role_id":      req.role_id,
            "service_name": req.service_name,
            "permission_id": row["id"],
            "can_start":     req.can_start,
            "can_use":       req.can_use,
            "can_force_stop": req.can_force_stop,
            "can_edit_config": req.can_edit_config,
        },
    )
    return {"permission_id": row["id"]}


@router.delete("/role-permissions/{permission_id}")
async def revoke_permission(permission_id: int,
                              admin: dict = Depends(_require_admin)) -> dict:
    row = await db.fetchrow(
        """
        DELETE FROM platform.role_permissions
        WHERE id = $1
        RETURNING role_id, service_name
        """,
        permission_id,
    )
    if not row:
        raise HTTPException(404, "Permission grant not found.")
    await audit.write(
        "role_permission_revoked", user_id=admin["user_id"],
        details={
            "permission_id": permission_id,
            "role_id":       row["role_id"],
            "service_name":  row["service_name"],
        },
    )
    return {"ok": True}
