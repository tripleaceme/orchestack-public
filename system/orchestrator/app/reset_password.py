"""Reset an operator's password from the host shell.

Recovery path for when an operator (particularly the sole administrator
on a fresh install) has forgotten their password and no other admin
exists to reset it for them via the Users page. Only someone with
shell access to the OrcheStack host can run this — which mirrors the
platform's threat model: root-on-host = root-on-platform.

Usage from the host:

    # Reset by email — recommended when the operator remembers their email
    docker exec orchestack-orchestrator python -m app.reset_password \\
        --email admin@example.com

    # Reset by username
    docker exec orchestack-orchestrator python -m app.reset_password \\
        --username ayoade

    # Pass an explicit new password instead of generating one
    docker exec orchestack-orchestrator python -m app.reset_password \\
        --email admin@example.com --password 'MyNewSecret!2026'

Prints the new password on stdout on success. Emits a
`password_reset_by_cli` event to the audit log so the reset is
traceable — an operator returning to the dashboard sees the event in
the Audit page and knows their password was rotated.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import string
import sys

import bcrypt

from . import audit, db


def _generate_password(length: int = 20) -> str:
    """Generate a URL-safe password with mixed classes.

    20 chars gives ~120 bits of entropy from the 65-char alphabet;
    well past bcrypt's cost=12 verification budget. Includes at least
    one of each character class so a strict downstream validator does
    not reject it (dashboard uses the same bcrypt path so it accepts
    anything, but explicit is better than accidentally weak).
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+="
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in candidate)
                and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)
                and any(c in "!@#$%^&*_-+=" for c in candidate)):
            return candidate


async def _reset(*, email: str | None, username: str | None,
                 new_password: str | None) -> int:
    """Locate the user + write the new bcrypt hash + audit-log the event.

    Returns process exit code: 0 on success, 1 on user-not-found,
    2 on database error.
    """
    if not email and not username:
        print("ERROR: pass --email or --username", file=sys.stderr)
        return 2

    try:
        await db.init_pool()
    except Exception as e:
        print(f"ERROR: could not connect to postgres — {e}", file=sys.stderr)
        return 2

    lookup_col = "email" if email else "username"
    lookup_val = email or username
    row = await db.fetchrow(
        f"SELECT id, username, email FROM platform.users WHERE {lookup_col} = $1",
        lookup_val,
    )
    if row is None:
        print(f"ERROR: no user with {lookup_col} = {lookup_val!r}", file=sys.stderr)
        return 1

    generated = new_password is None
    if generated:
        new_password = _generate_password()

    hashed = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode()
    await db.execute(
        "UPDATE platform.users SET password_hash = $1, updated_at = now() WHERE id = $2",
        hashed, row["id"],
    )

    # Audit log — actor is the user themselves (they're the CLI operator
    # and the affected user; the CLI has no distinct actor identity).
    await audit.write(
        "password_reset_by_cli",
        user_id=row["id"],
        details={
            "target_username": row["username"],
            "target_email": row["email"],
            "generated_new_password": generated,
        },
    )

    print()
    print("=" * 60)
    print(f"  Password reset for user id={row['id']} ({row['username']})")
    print(f"  Email:    {row['email']}")
    if generated:
        print(f"  New password (generated): {new_password}")
        print()
        print("  Sign in with this password, then rotate it from")
        print("  the Profile page as soon as possible.")
    else:
        print("  New password: (as supplied)")
    print("=" * 60)
    print()

    await db.close_pool()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.reset_password",
        description="Reset an OrcheStack operator's password from the host shell.",
    )
    parser.add_argument("--email",    help="Email of the user to reset")
    parser.add_argument("--username", help="Username of the user to reset")
    parser.add_argument("--password", help="New password (omit to auto-generate)")
    args = parser.parse_args()

    exit_code = asyncio.run(_reset(
        email=args.email,
        username=args.username,
        new_password=args.password,
    ))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
