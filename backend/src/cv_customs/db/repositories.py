"""Repository helpers.

Users, audit log and uploaded resume files. All helpers ``flush`` but never
``commit`` — transaction boundaries belong to the request handlers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog, Resume, User


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def create_user_with_email(
    session: AsyncSession,
    *,
    email: str,
    password_hash: str,
    consent_given_at: datetime | None = None,
    cross_border_consent_at: datetime | None = None,
) -> User:
    user = User(
        email=email,
        password_hash=password_hash,
        consent_given_at=consent_given_at,
        cross_border_consent_at=cross_border_consent_at,
    )
    session.add(user)
    await session.flush()
    return user


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_email_ci(session: AsyncSession, email: str) -> User | None:
    """Case-insensitive email lookup.

    Email addresses should be treated case-insensitively per RFC 5321. We
    keep the legacy :func:`get_user_by_email` for exact lookups (used by
    callers that have already normalised) and use this helper on the
    sign-in / password-reset paths where the client-supplied casing is
    untrusted.
    """

    result = await session.execute(
        select(User).where(func.lower(User.email) == email.lower())
    )
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def set_password_hash(
    session: AsyncSession, *, user_id: uuid.UUID, password_hash: str
) -> None:
    """Update a user's password hash and stamp ``password_updated_at``."""

    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            password_hash=password_hash,
            password_updated_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()


async def update_user_email(
    session: AsyncSession, *, user_id: uuid.UUID, email: str
) -> None:
    await session.execute(update(User).where(User.id == user_id).values(email=email))
    await session.flush()


async def set_consent_given_at(
    session: AsyncSession, *, user_id: uuid.UUID, at: datetime
) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(consent_given_at=at)
    )
    await session.flush()


async def set_cross_border_consent_at(
    session: AsyncSession, *, user_id: uuid.UUID, at: datetime | None
) -> None:
    """``at=None`` clears the mark — cross-border consent is revocable
    without touching the account, unlike the primary consent."""

    await session.execute(
        update(User).where(User.id == user_id).values(cross_border_consent_at=at)
    )
    await session.flush()


async def hard_delete_user(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Permanently delete user and all associated data (resume rows cascade).

    Returns True if user existed and was deleted. S3 blobs are cleaned up
    by the caller — the repository layer has no storage access.
    """

    user = await get_user_by_id(session, user_id)
    if not user:
        return False
    await session.delete(user)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# MFA secrets (feature-flagged)
# ---------------------------------------------------------------------------


async def set_mfa_secret(
    session: AsyncSession, *, user_id: uuid.UUID, secret: str
) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(mfa_secret=secret)
    )
    await session.flush()


async def clear_mfa_secret(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(mfa_secret=None)
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Resume files
# ---------------------------------------------------------------------------


async def create_resume_file(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    filename: str,
    mime: str,
    size_bytes: int,
    blob_key: str,
    resume_id: uuid.UUID | None = None,
) -> Resume:
    """Insert the metadata row for an uploaded file.

    ``resume_id`` is accepted explicitly because the upload handler needs
    the id *before* the insert — the S3 key embeds it.
    """

    resume = Resume(
        id=resume_id or uuid.uuid4(),
        user_id=user_id,
        filename=filename,
        mime=mime,
        size_bytes=size_bytes,
        blob_key=blob_key,
    )
    session.add(resume)
    await session.flush()
    return resume


async def get_resume(
    session: AsyncSession, *, resume_id: uuid.UUID, user_id: uuid.UUID
) -> Resume | None:
    """Owner-scoped fetch — other users' resumes look like 404s."""

    result = await session.execute(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def list_user_resumes(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Resume], int]:
    total = (
        await session.execute(
            select(func.count()).select_from(Resume).where(Resume.user_id == user_id)
        )
    ).scalar_one()
    result = await session.execute(
        select(Resume)
        .where(Resume.user_id == user_id)
        .order_by(Resume.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total)


async def delete_resume(
    session: AsyncSession, *, resume_id: uuid.UUID, user_id: uuid.UUID
) -> Resume | None:
    """Hard-delete the metadata row. Returns the deleted row (for its
    ``blob_key``) or ``None`` when it doesn't exist / isn't owned."""

    resume = await get_resume(session, resume_id=resume_id, user_id=user_id)
    if resume is None:
        return None
    await session.delete(resume)
    await session.flush()
    return resume


# ---------------------------------------------------------------------------
# Personal data export (152-FZ art. 14 — right of access)
# ---------------------------------------------------------------------------


async def get_user_personal_data(session: AsyncSession, user_id: uuid.UUID) -> dict:
    """Collect all personal data for a user into a serializable dict."""

    user = await get_user_by_id(session, user_id)
    if not user:
        return {}

    resumes, _total = await list_user_resumes(
        session, user_id=user_id, offset=0, limit=1000
    )

    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "consent_given_at": user.consent_given_at.isoformat()
            if user.consent_given_at
            else None,
            "cross_border_consent_at": user.cross_border_consent_at.isoformat()
            if user.cross_border_consent_at
            else None,
            "created_at": user.created_at.isoformat(),
        },
        "resumes": [
            {
                "id": str(r.id),
                "filename": r.filename,
                "mime": r.mime,
                "size_bytes": r.size_bytes,
                "created_at": r.created_at.isoformat(),
            }
            for r in resumes
        ],
    }


# ---------------------------------------------------------------------------
# Audit log (PP RF #1119, FSTEC Order #21)
# ---------------------------------------------------------------------------


async def create_audit_log(
    session: AsyncSession,
    *,
    user_id: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
    details: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        details=details,
    )
    session.add(entry)
    await session.flush()
    return entry
