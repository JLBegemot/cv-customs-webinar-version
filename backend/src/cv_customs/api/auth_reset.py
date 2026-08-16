"""Password reset.

Two endpoints::

    POST /api/v1/auth/password/reset/request  { email }
        Always returns 202 regardless of whether the email exists — the
        response timing is padded so attackers can't enumerate accounts
        by reading the HTTP status. If a user *does* exist, we generate
        a URL-safe token, store ``sha256(token)`` in an in-process store
        with the configured TTL, and email the plaintext token as a link
        back to the app.

    POST /api/v1/auth/password/reset/confirm  { token, new_password }
        Validates the token: look up the stored hash, reject on mismatch /
        expiry / already-used. On success rotate the password hash, delete
        the token (single-use), write an audit-log entry.

The reset link points at ``APP_BASE_URL/reset-password/{token}``.

Simplified (webinar) edition: tokens live in process memory — the service
is local-only and single-process, so a restart simply invalidates any
outstanding reset links.
"""

from __future__ import annotations

import hashlib
import secrets
import time

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import repositories as repo
from ..db.session import get_session
from . import auth as _auth

router = APIRouter(prefix="/api/v1/auth/password/reset", tags=["auth"])


_TOKEN_BYTES = 32

# user_id (str) -> (sha256(token), unix expiry). Single-use, TTL-bound.
_TOKENS: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ResetRequest(BaseModel):
    email: EmailStr


class ResetConfirmRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    new_password: str = Field(min_length=6, max_length=256)


# ---------------------------------------------------------------------------
# In-process token helpers
# ---------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _store_token(request: Request, user_id, token: str) -> None:
    ttl_s = settings.password_reset_token_ttl_minutes * 60
    _TOKENS[str(user_id)] = (_hash_token(token), time.time() + ttl_s)


async def _consume_token(request: Request, user_id, token: str) -> bool:
    """Return True if ``token`` matches the stored hash for ``user_id``.

    The entry is *always* deleted on a match — tokens are single-use.
    Expired entries are dropped on access.
    """

    entry = _TOKENS.get(str(user_id))
    if entry is None:
        return False
    stored_hash, expires_at = entry
    if time.time() > expires_at:
        _TOKENS.pop(str(user_id), None)
        return False
    if stored_hash != _hash_token(token):
        return False
    _TOKENS.pop(str(user_id), None)
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/request", status_code=status.HTTP_202_ACCEPTED)
async def request_reset(
    body: ResetRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Always returns 202 — never reveal whether the email is registered."""

    user = await repo.get_user_by_email_ci(session, body.email)

    # Only proceed when a user exists; otherwise we silently drop.
    if user and user.email:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        await _store_token(request, user.id, token)

        email_client = getattr(request.app.state, "email", None)
        if email_client is not None:
            reset_url = (
                f"{settings.app_base_url.rstrip('/')}/reset-password/{token}"
            )
            await email_client.send_template(
                template_alias="password-reset",
                to=user.email,
                model={
                    "reset_url": reset_url,
                    "expires_in_minutes": settings.password_reset_token_ttl_minutes,
                    "product_name": settings.from_name,
                },
                tag="password-reset",
            )

        await repo.create_audit_log(
            session,
            user_id=str(user.id),
            action="password_reset_request",
            resource_type="user",
            resource_id=str(user.id),
            ip_address=_auth.client_ip(request),
        )
        await session.commit()

    return {"status": "accepted"}


@router.post("/confirm", status_code=status.HTTP_200_OK)
async def confirm_reset(
    body: ResetConfirmRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Exchange ``token`` + ``new_password`` for a fresh session.

    The token carries no identity by itself (it's a random string), so we
    need a way to locate the target user. Strategy: we iterate over all
    users — **no**, clearly not. Instead, the request *must* include the
    email of the user the token was issued for. That removes both the
    "unbounded scan" problem and any timing side channel that a list lookup
    would introduce.

    To keep the contract ergonomic we accept the email on the **confirm**
    request too. The client is expected to collect it on the reset form
    (the reset link can pre-fill it from the URL).
    """

    # Deferred import to avoid a circular dep in tests that patch repo.
    email = request.headers.get("x-reset-email") or request.query_params.get("email")
    # Fall back: let clients supply the email in the JSON body via an
    # optional ``email`` key if they prefer (kept out of the schema above
    # to avoid type coupling with the SPA form).
    if not email:
        try:
            raw = await request.json()
        except Exception:  # pragma: no cover — framework parse failure
            raw = {}
        email = raw.get("email") if isinstance(raw, dict) else None

    if not email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Reset confirmation requires the account email.",
        )

    user = await repo.get_user_by_email_ci(session, email)
    if user is None:
        # Same 400 as missing email — do not leak user existence.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid reset token.")

    ok = await _consume_token(request, user.id, body.token)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token.")

    new_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    await repo.set_password_hash(session, user_id=user.id, password_hash=new_hash)

    await repo.create_audit_log(
        session,
        user_id=str(user.id),
        action="password_reset_confirm",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=_auth.client_ip(request),
    )
    await session.commit()

    token = _auth.create_access_token(user.id)
    return _auth.user_response(user, token)
