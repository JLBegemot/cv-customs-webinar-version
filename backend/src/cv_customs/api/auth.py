"""Authentication endpoints + shared helpers (email + password only).

Simplified (webinar) edition — local-only service, so there is no rate
limiting and no email-verification loop: registration activates the account
immediately and returns a JWT.

* this file        — legacy ``/api/auth/{register,login}`` routes +
                     shared helpers (JWT, password hashing,
                     ``get_current_user``) re-used by every other module
                     in the package.
* ``auth_reset``   — password reset (``/api/v1/auth/password/reset/...``).
* ``auth_mfa``     — TOTP MFA stub, gated by ``FEATURE_MFA``.
* ``auth_v1``      — aggregator that mounts the v1 versions of
                     register/login/logout on the new ``/api/v1/auth`` prefix.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import (
    JWT_ALGORITHM,
    JWT_EXPIRE_HOURS,
    JWT_SECRET,
)
from ..db import repositories as repo
from ..db.session import get_session
from ..infra.request import client_ip

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_bearer = HTTPBearer()


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def _client_ip(request: Request) -> str:
    """Legacy alias — new code should import ``client_ip`` from infra."""

    return client_ip(request)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def create_access_token(user_id: uuid.UUID) -> str:
    """Issue a signed JWT for ``user_id``.

    Public helper (was ``_create_access_token``) — re-used by the
    email-verification and password-reset flows, so we drop the leading
    underscore.
    """

    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "exp": now + timedelta(hours=JWT_EXPIRE_HOURS),
        "iat": now,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# Backwards-compatible alias (old tests import the private name).
_create_access_token = create_access_token


def user_response(user, token: str) -> dict:
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "consent_given_at": (
                user.consent_given_at.isoformat() if user.consent_given_at else None
            ),
            "cross_border_consent_at": (
                user.cross_border_consent_at.isoformat()
                if user.cross_border_consent_at
                else None
            ),
        },
    }


_user_response = user_response  # keep the old private name working


# ---------------------------------------------------------------------------
# Dependency: get current user from JWT
# ---------------------------------------------------------------------------


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    user = await repo.get_user_by_id(session, user_id)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


# ---------------------------------------------------------------------------
# Email / Password
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    consent: bool = False
    cross_border_consent: bool = False


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/register")
async def register(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not body.consent:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Consent to personal data processing is required (152-FZ)",
        )

    existing = await repo.get_user_by_email_ci(session, body.email)
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    if len(body.password) < 6:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Password must be at least 6 characters",
        )

    now = datetime.now(timezone.utc)
    hashed = _hash_password(body.password)
    user = await repo.create_user_with_email(
        session,
        email=body.email.lower(),
        password_hash=hashed,
        consent_given_at=now,
        cross_border_consent_at=now if body.cross_border_consent else None,
    )
    # Stamp password_updated_at so forced rotations later can use it.
    await repo.set_password_hash(session, user_id=user.id, password_hash=hashed)

    await repo.create_audit_log(
        session,
        user_id=str(user.id),
        action="register",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=_client_ip(request),
        details="email registration",
    )
    await session.commit()

    # No email-verification loop in the simplified edition — the account is
    # live immediately.
    token = create_access_token(user.id)
    return user_response(user, token)


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = await repo.get_user_by_email_ci(session, body.email)
    if not user or not user.password_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    if not _verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    await repo.create_audit_log(
        session,
        user_id=str(user.id),
        action="login",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=_client_ip(request),
    )
    await session.commit()

    token = create_access_token(user.id)
    return user_response(user, token)


