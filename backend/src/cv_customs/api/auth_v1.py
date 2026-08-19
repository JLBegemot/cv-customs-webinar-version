"""v1 auth router — aliases the legacy ``/api/auth/{register,login}``
handlers under ``/api/v1/auth/*`` (Phase-1 MVP, Week 2).

The handlers themselves live in :mod:`cv_customs.api.auth`. Duplicating
the route registrations with a different prefix gives us a stable v1
surface to evolve under while keeping the legacy URLs alive for one
deploy cycle — SPA and mobile clients can migrate at their own pace.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import get_session
from . import auth as _auth

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register")
async def register_v1(
    body: _auth.RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await _auth.register(body, request, session)


@router.post("/login")
async def login_v1(
    body: _auth.LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await _auth.login(body, request, session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_v1(
    credentials: HTTPAuthorizationCredentials = Depends(_auth._bearer),
):
    """Client-side JWT invalidation.

    We still expose the endpoint so SPAs have a stable logout URL. It is
    a no-op; the client is expected to discard the access token on its side.
    """

    # Intentional no-op: there is no server-side session store.
    # Returning 204 keeps the client contract stable —
    # the client is expected to discard the access token on its side.
    return None
