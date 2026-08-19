"""SQLAlchemy models (webinar edition).

Three tables only:

* ``users``      — email/password accounts + 152-FZ consent timestamps.
* ``audit_log``  — immutable log of personal-data access events.
* ``resumes``    — uploaded files: metadata row + S3 blob key, stored as-is.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(unique=True)
    password_hash: Mapped[str | None]
    consent_given_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    cross_border_consent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # TOTP secret, nullable — used by the FEATURE_MFA-gated endpoints.
    mfa_secret: Mapped[str | None] = mapped_column(Text, default=None)
    password_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    resumes: Mapped[list["Resume"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuditLog(Base):
    """Immutable log of personal-data access events (152-FZ compliance)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(36))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Resume(Base):
    """An uploaded resume file: metadata row + pointer to the S3 blob.

    Files are stored as-is. ``blob_key`` points at
    ``resumes/{resume_id}/original.{ext}`` in the configured bucket.
    """

    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    blob_key: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="resumes")
