"""User management endpoints (152-FZ: right of access, consent revocation, account deletion)."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import repositories as repo
from ..db.models import User
from ..db.session import get_session
from ..infra.request import client_ip
from .auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["user"])


class UserMeResponse(BaseModel):
    id: str
    email: str | None
    consent_given_at: str | None
    cross_border_consent_at: str | None
    created_at: str


# ---------------------------------------------------------------------------
# GET /api/user/me
# ---------------------------------------------------------------------------


def _me_response(user: User) -> UserMeResponse:
    return UserMeResponse(
        id=str(user.id),
        email=user.email,
        consent_given_at=user.consent_given_at.isoformat()
        if user.consent_given_at
        else None,
        cross_border_consent_at=user.cross_border_consent_at.isoformat()
        if user.cross_border_consent_at
        else None,
        created_at=user.created_at.isoformat(),
    )


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)) -> UserMeResponse:
    return _me_response(user)


# ---------------------------------------------------------------------------
# PATCH /api/user/me — partial profile update (email, consent marks)
# ---------------------------------------------------------------------------


class UpdateMeRequest(BaseModel):
    email: EmailStr | None = None
    consent: bool | None = None
    cross_border_consent: bool | None = None


@router.patch("/me")
async def update_me(
    body: UpdateMeRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> UserMeResponse:
    if (
        body.email is None
        and body.consent is None
        and body.cross_border_consent is None
    ):
        raise HTTPException(422, {"code": "NOTHING_TO_UPDATE"})

    # Revoking the primary consent is legally equivalent to deleting the
    # account (152-FZ art. 9 p. 2) — that must stay an explicit, separate
    # action, not a side effect of a partial update.
    if body.consent is False:
        raise HTTPException(
            422,
            {
                "code": "CONSENT_REVOKE_FORBIDDEN",
                "hint": "Use POST /api/user/revoke-consent — revoking consent deletes the account.",
            },
        )

    ip = client_ip(request)

    if body.email is not None:
        new_email = str(body.email)
        old_email = user.email
        if (old_email or "").lower() != new_email.lower():
            existing = await repo.get_user_by_email_ci(session, new_email)
            if existing is not None and existing.id != user.id:
                raise HTTPException(409, {"code": "EMAIL_TAKEN"})
            await repo.update_user_email(session, user_id=user.id, email=new_email)
            await repo.create_audit_log(
                session,
                user_id=str(user.id),
                action="email_changed",
                resource_type="user",
                resource_id=str(user.id),
                ip_address=ip,
                details=f"{old_email} -> {new_email}",
            )

    now = datetime.now(timezone.utc)

    if body.consent is True and user.consent_given_at is None:
        await repo.set_consent_given_at(session, user_id=user.id, at=now)
        await repo.create_audit_log(
            session,
            user_id=str(user.id),
            action="consent_granted",
            resource_type="user",
            resource_id=str(user.id),
            ip_address=ip,
        )

    if body.cross_border_consent is True and user.cross_border_consent_at is None:
        await repo.set_cross_border_consent_at(session, user_id=user.id, at=now)
        await repo.create_audit_log(
            session,
            user_id=str(user.id),
            action="cross_border_consent_granted",
            resource_type="user",
            resource_id=str(user.id),
            ip_address=ip,
        )
    elif (
        body.cross_border_consent is False and user.cross_border_consent_at is not None
    ):
        await repo.set_cross_border_consent_at(session, user_id=user.id, at=None)
        await repo.create_audit_log(
            session,
            user_id=str(user.id),
            action="cross_border_consent_revoked",
            resource_type="user",
            resource_id=str(user.id),
            ip_address=ip,
        )

    await session.commit()
    return _me_response(user)


# ---------------------------------------------------------------------------
# GET /api/user/personal-data  (152-FZ art. 14 — right of access)
# ---------------------------------------------------------------------------


@router.get("/personal-data")
async def get_personal_data(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    await repo.create_audit_log(
        session,
        user_id=str(user.id),
        action="personal_data_export",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=client_ip(request),
    )
    data = await repo.get_user_personal_data(session, user.id)
    await session.commit()
    return data


# ---------------------------------------------------------------------------
# POST /api/user/revoke-consent  (152-FZ art. 9 p. 2 — consent revocation)
# ---------------------------------------------------------------------------


class RevokeConsentRequest(BaseModel):
    confirm: bool = False


@router.post("/revoke-consent")
async def revoke_consent(
    body: RevokeConsentRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if not body.confirm:
        raise HTTPException(
            422, "You must confirm consent revocation by setting confirm=true"
        )

    await repo.create_audit_log(
        session,
        user_id=str(user.id),
        action="consent_revoked",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=client_ip(request),
    )

    deleted = await repo.hard_delete_user(session, user.id)
    if not deleted:
        raise HTTPException(404, "User not found")

    await repo.create_audit_log(
        session,
        user_id="[deleted]",
        action="account_deleted_after_consent_revocation",
        resource_type="user",
        resource_id=str(user.id),
        ip_address=client_ip(request),
    )
    await session.commit()

    return {"ok": True, "message": "Consent revoked and all personal data deleted"}


# ---------------------------------------------------------------------------
# DELETE /api/user/account  (152-FZ art. 21 — right to erasure)
# ---------------------------------------------------------------------------


class DeleteAccountRequest(BaseModel):
    confirm: bool = False


@router.delete("/account")
async def delete_account(
    body: DeleteAccountRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if not body.confirm:
        raise HTTPException(
            422, "You must confirm account deletion by setting confirm=true"
        )

    user_id_str = str(user.id)

    await repo.create_audit_log(
        session,
        user_id=user_id_str,
        action="account_deletion_requested",
        resource_type="user",
        resource_id=user_id_str,
        ip_address=client_ip(request),
    )

    deleted = await repo.hard_delete_user(session, user.id)
    if not deleted:
        raise HTTPException(404, "User not found")

    await repo.create_audit_log(
        session,
        user_id="[deleted]",
        action="account_deleted",
        resource_type="user",
        resource_id=user_id_str,
        ip_address=client_ip(request),
    )
    await session.commit()

    log.info("Account deleted: user_id=%s", user_id_str)
    return {
        "ok": True,
        "message": "Account and all associated data permanently deleted",
    }
