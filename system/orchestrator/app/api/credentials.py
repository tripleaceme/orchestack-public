"""Credentials API — read + update the operator's `.env`.

The orchestrator owns the operator's `.env` (bind-mounted at
config.ENV_FILE — see system/docker/docker-compose.yml). This module
exposes:

    GET  /api/credentials             → list all variables, with sensitivity
                                        flags and a coarse `editable` hint
    PUT  /api/credentials/{key}       → update a single variable; persists
                                        back to the bind-mounted file

Why a per-key PUT rather than a bulk POST: each update produces an
audit-log row and the partial-update semantics let the dashboard's
HTMX swap target a single row at a time.

Sensitivity / editability is decided from the variable NAME, not from
its value:
  - Always read-only: `*_TAG` (image tags — bundle-update concern, not
    a credential rotation), and `ORCHESTACK_DB_PASSWORD` (rotating
    breaks the platform without a co-ordinated restart; M4 will add a
    proper rotation flow).
  - Sensitive (masked in GET unless `?reveal=true`): anything matching
    `*_PASSWORD`, `*_PASS`, `*_SECRET`, `*_KEY`, `*_TOKEN`.
  - Everything else: plain value.

The .env file is parsed and rewritten with care:
  - Comments and blank lines are preserved.
  - Variable VALUES are rewritten in place — line numbers stay stable.
  - Unknown / unexpected keys in the file are returned to the dashboard
    so the operator can see EVERYTHING in their .env, not just what
    OrcheStack knows about.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import audit, config

log = logging.getLogger("orchestrator.credentials")

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


# Read-only keys — the dashboard renders these as informational only.
READ_ONLY_PATTERNS = (
    re.compile(r"_TAG$"),                  # AUTH_TAG, ORCHESTRATOR_TAG, …
    re.compile(r"^ORCHESTACK_DB_PASSWORD$"),  # rotating breaks the platform
)

# Sensitive patterns — values masked in GET responses unless reveal=true.
SENSITIVE_PATTERNS = (
    re.compile(r"_PASSWORD$"),
    re.compile(r"_PASS$"),
    re.compile(r"_SECRET$"),
    re.compile(r"_KEY$"),
    re.compile(r"_TOKEN$"),
    re.compile(r"^ORCHESTACK_DB_PASSWORD$"),
)

LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _is_readonly(key: str) -> bool:
    return any(p.search(key) for p in READ_ONLY_PATTERNS)


def _is_sensitive(key: str) -> bool:
    return any(p.search(key) for p in SENSITIVE_PATTERNS)


def _iter_env_lines() -> Iterator[tuple[int, str]]:
    """Yield (line_no, raw_line) for the operator's .env, 1-indexed."""
    p = Path(config.ENV_FILE)
    if not p.exists():
        return
    for i, line in enumerate(p.read_text().splitlines(), start=1):
        yield i, line


@router.get("")
async def list_credentials(
    reveal: bool = Query(False, description="If true, return raw values for sensitive keys"),
) -> dict[str, object]:
    """Return every variable in .env with sensitivity + editability metadata."""
    items: list[dict[str, object]] = []
    for _line_no, line in _iter_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = LINE_RE.match(stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        sensitive = _is_sensitive(key)
        readonly = _is_readonly(key)
        displayed = value if (not sensitive or reveal) else ("•" * 10 if value else "")
        items.append({
            "key": key,
            "value": displayed,
            "is_sensitive": sensitive,
            "is_readonly": readonly,
            "is_set": bool(value),
        })
    return {"credentials": items, "env_file": config.ENV_FILE}


class UpdateRequest(BaseModel):
    value: str = Field(..., description="New value for the variable. Empty string clears it.")
    actor_user_id: int | None = Field(None, description="User triggering the change.")


@router.put("/{key}")
async def update_credential(key: str, req: UpdateRequest) -> dict[str, object]:
    """Update a single variable in .env. Read-only keys are rejected.

    Audit log captures the change with the key but NEVER the value — we
    don't want secrets surfacing in the audit table. The presence of an
    update is sufficient evidence.
    """
    if not LINE_RE.match(f"{key}=x"):
        raise HTTPException(400, f"invalid key shape: {key!r}")
    if _is_readonly(key):
        raise HTTPException(409, f"{key} is not editable from the dashboard")

    p = Path(config.ENV_FILE)
    if not p.exists():
        raise HTTPException(500, f"env file not found at {config.ENV_FILE}")

    lines = p.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.strip())
        if m and m.group(1) == key:
            lines[i] = f"{key}={req.value}"
            updated = True
            break

    if not updated:
        # Append at the end — new key.
        lines.append(f"{key}={req.value}")

    p.write_text("\n".join(lines) + "\n")

    await audit.write(
        "credential_updated",
        user_id=req.actor_user_id,
        details={"key": key, "was_set": updated, "appended": not updated},
    )

    return {
        "ok": True,
        "key": key,
        "is_sensitive": _is_sensitive(key),
    }
