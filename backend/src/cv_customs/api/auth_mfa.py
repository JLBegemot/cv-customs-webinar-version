"""TOTP MFA stub (Phase-1 MVP, Week 2).

Three endpoints are shipped *behind* ``FEATURE_MFA``:

* ``POST /api/v1/auth/mfa/enroll`` — generate a TOTP secret, persist it
  on ``users.mfa_secret``, return a ``provisioning_uri`` and a
  base-64 ``qr_png`` data URL for the authenticator app.

* ``POST /api/v1/auth/mfa/verify`` — verify a 6-digit code against the
  stored secret. Returns 204 on success. Intended as the "confirm your
  new MFA device" step; enforcement on login is not wired.

* ``POST /api/v1/auth/mfa/recovery-codes/regenerate`` — rotate the
  10 one-time recovery codes. We only return the *plaintext* codes
  once, directly in the response; the stored payload is a SHA-256 hash
  list. The hashes live in process memory,
  so a restart drops them — acceptable for a local-only service.

When ``FEATURE_MFA`` is ``false`` the entire router is **not mounted**
— the endpoints 404 by virtue of not existing.
"""

from __future__ import annotations

import base64
import hashlib
import io
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import repositories as repo
from ..db.session import get_session
from . import auth as _auth

router = APIRouter(prefix="/api/v1/auth/mfa", tags=["auth", "mfa"])


_RECOVERY_COUNT = 10
_MFA_ISSUER = "CV Customs"

# user_id (str) -> set of SHA-256 hashes of the active recovery codes.
_RECOVERY_CODES: dict[str, set[str]] = {}


class EnrollResponse(BaseModel):
    provisioning_uri: str
    qr_png_data_url: str
    recovery_codes: list[str]


class VerifyRequest(BaseModel):
    code: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _generate_recovery_codes() -> list[str]:
    # 10-char URL-safe strings; plenty of entropy for "last-resort" codes.
    return [secrets.token_urlsafe(8) for _ in range(_RECOVERY_COUNT)]


async def _store_recovery_codes(request: Request, user_id, codes: list[str]) -> None:
    # Replace the whole set so regeneration invalidates the old codes.
    _RECOVERY_CODES[str(user_id)] = {_hash_code(c) for c in codes}


def _build_qr_png_data_url(provisioning_uri: str) -> str:
    """Encode the otpauth URI into a PNG data URL.

    ``qrcode`` and Pillow are optional deps — if either is missing we still
    return the provisioning URI in a useful form, just without the PNG.
    Authenticator apps accept plain text URIs too.
    """

    try:
        import qrcode  # type: ignore import-not-found

        img = qrcode.make(provisioning_uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Routes — only mounted when FEATURE_MFA is true
# ---------------------------------------------------------------------------


@router.post("/enroll", response_model=EnrollResponse)
async def enroll(
    request: Request,
    current_user=Depends(_auth.get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EnrollResponse:
    """Generate a fresh TOTP secret for ``current_user``.

    Overwrites any existing secret — users who lose their device are
    expected to enrol a fresh one after signing in via email / Google
    and going through the account-recovery flow.
    """

    import pyotp  # type: ignore import-not-found

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    label = current_user.email or f"user:{current_user.id}"
    provisioning_uri = totp.provisioning_uri(name=label, issuer_name=_MFA_ISSUER)

    await repo.set_mfa_secret(session, user_id=current_user.id, secret=secret)
    await repo.create_audit_log(
        session,
        user_id=str(current_user.id),
        action="mfa_enroll",
        resource_type="user",
        resource_id=str(current_user.id),
        ip_address=_auth.client_ip(request),
    )
    await session.commit()

    recovery = _generate_recovery_codes()
    await _store_recovery_codes(request, current_user.id, recovery)

    return EnrollResponse(
        provisioning_uri=provisioning_uri,
        qr_png_data_url=_build_qr_png_data_url(provisioning_uri),
        recovery_codes=recovery,
    )


@router.post("/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify(
    body: VerifyRequest,
    request: Request,
    current_user=Depends(_auth.get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    import pyotp  # type: ignore import-not-found

    if not current_user.mfa_secret:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "MFA is not enrolled for this account."
        )

    totp = pyotp.TOTP(current_user.mfa_secret)
    # ``valid_window=1`` tolerates a single 30-second drift either way —
    # standard practice for TOTP.
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid MFA code")

    await repo.create_audit_log(
        session,
        user_id=str(current_user.id),
        action="mfa_verify",
        resource_type="user",
        resource_id=str(current_user.id),
        ip_address=_auth.client_ip(request),
    )
    await session.commit()


@router.post("/recovery-codes/regenerate")
async def regenerate_recovery_codes(
    request: Request,
    current_user=Depends(_auth.get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not current_user.mfa_secret:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "MFA is not enrolled for this account."
        )
    recovery = _generate_recovery_codes()
    await _store_recovery_codes(request, current_user.id, recovery)

    await repo.create_audit_log(
        session,
        user_id=str(current_user.id),
        action="mfa_recovery_regenerate",
        resource_type="user",
        resource_id=str(current_user.id),
        ip_address=_auth.client_ip(request),
    )
    await session.commit()
    return {"recovery_codes": recovery}


def mount_if_enabled(app) -> None:
    """Register the MFA router only when the feature flag is on.

    Keeps the stub strictly invisible in prod until the feature is ready —
    a missing flag returns 404 instead of a 503, matching OpenAPI docs.
    """

    if settings.feature_mfa:
        app.include_router(router)
