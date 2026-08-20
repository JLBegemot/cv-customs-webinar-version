"""Uploaded-resume file storage.

The service stores resume files **as-is**. The surface is a plain file store:

* ``POST   /api/v1/resumes/upload``        — validate + store a PDF/DOCX
* ``GET    /api/v1/resumes``               — list the user's files
* ``GET    /api/v1/resumes/{id}``          — metadata for one file
* ``GET    /api/v1/resumes/{id}/original`` — download the original blob
* ``DELETE /api/v1/resumes/{id}``          — delete metadata + blob

High-level flow for ``POST /upload``:

1. Read the multipart body (cap at ``RESUME_UPLOAD_MAX_BYTES``).
2. Sniff the MIME from the first few bytes — ignore the client-supplied
   ``filename`` / ``Content-Type`` entirely.
3. Persist the blob to S3 under ``resumes/{id}/original.{ext}``. S3 is
   **required**: without the blob an upload is meaningless, so an S3
   outage maps to 503 instead of a silent metadata-only insert.
4. Insert the ``resumes`` metadata row and commit.

Errors map to typed codes:
``UPLOAD_UNSUPPORTED_FORMAT`` (422), ``UPLOAD_TOO_LARGE`` (413),
``UPLOAD_EMPTY`` (422).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import repositories as repo
from ..db.models import User
from ..db.session import get_session
from ..infra.request import client_ip as _client_ip
from ..infra.storage import S3Client, get_s3_optional
from ..utils.mime import expected_extension, sniff_mime
from .auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/resumes", tags=["resumes"])

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _max_upload_bytes() -> int:
    return getattr(settings, "resume_upload_max_bytes", _DEFAULT_MAX_BYTES)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ResumeFileOut(BaseModel):
    id: str
    filename: str
    mime: str
    size_bytes: int
    created_at: str


class ResumeList(BaseModel):
    items: list[ResumeFileOut]
    total: int
    page: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(ts: datetime | None) -> str:
    return (ts or datetime.now(timezone.utc)).isoformat()


def _file_out(r) -> ResumeFileOut:
    return ResumeFileOut(
        id=str(r.id),
        filename=r.filename,
        mime=r.mime,
        size_bytes=r.size_bytes,
        created_at=_iso(r.created_at),
    )


def _require_s3(s3: S3Client | None) -> S3Client:
    if s3 is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "File storage is unavailable. Try again later.",
        )
    return s3


async def _load_resume(session: AsyncSession, *, resume_id: str, user: User):
    try:
        rid = uuid.UUID(resume_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resume not found")
    resume = await repo.get_resume(session, resume_id=rid, user_id=user.id)
    if not resume:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resume not found")
    return resume


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_resume(
    request: Request,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    s3: S3Client | None = Depends(get_s3_optional),
) -> ResumeFileOut:
    storage = _require_s3(s3)

    max_bytes = _max_upload_bytes()
    # Read one byte past the cap so we can distinguish "exactly at the
    # limit" from "over it" without buffering arbitrarily large bodies.
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            {"code": "UPLOAD_TOO_LARGE", "max_bytes": max_bytes},
        )
    if not content:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "UPLOAD_EMPTY"},
        )

    mime = sniff_mime(content)
    if mime == "unknown":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"code": "UPLOAD_UNSUPPORTED_FORMAT", "supported": ["pdf", "docx"]},
        )

    ext = expected_extension(mime)
    resume_id = uuid.uuid4()
    blob_key = f"resumes/{resume_id}/original.{ext}"

    try:
        await storage.put_object(blob_key, content, content_type=mime)
    except Exception:
        log.exception("resume upload: S3 put_object failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "File storage is unavailable. Try again later.",
        )

    filename = (file.filename or f"resume.{ext}")[:255]
    resume = await repo.create_resume_file(
        session,
        user_id=user.id,
        filename=filename,
        mime=mime,
        size_bytes=len(content),
        blob_key=blob_key,
        resume_id=resume_id,
    )
    await repo.create_audit_log(
        session,
        user_id=str(user.id),
        action="resume_upload",
        resource_type="resume",
        resource_id=str(resume.id),
        ip_address=_client_ip(request),
        details=f"{mime}, {len(content)} bytes",
    )
    await session.commit()

    return _file_out(resume)


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------


@router.get("")
async def list_resumes(
    page: int = 0,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ResumeList:
    PAGE = 20
    resumes, total = await repo.list_user_resumes(
        session, user_id=user.id, offset=page * PAGE, limit=PAGE
    )
    return ResumeList(items=[_file_out(r) for r in resumes], total=total, page=page)


@router.get("/{resume_id}")
async def get_resume_detail(
    resume_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ResumeFileOut:
    resume = await _load_resume(session, resume_id=resume_id, user=user)
    return _file_out(resume)


# ---------------------------------------------------------------------------
# Download original
# ---------------------------------------------------------------------------


@router.get("/{resume_id}/original")
async def download_original(
    resume_id: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    s3: S3Client | None = Depends(get_s3_optional),
) -> Response:
    resume = await _load_resume(session, resume_id=resume_id, user=user)
    storage = _require_s3(s3)

    try:
        body = await storage.get_object(resume.blob_key)
    except Exception:
        log.exception("resume download: S3 get_object failed for %s", resume.blob_key)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "File storage is unavailable. Try again later.",
        )

    # RFC 5987 encoding is overkill here — filenames were capped and come
    # from our own DB row; strip quotes to keep the header well-formed.
    safe_name = resume.filename.replace('"', "")
    return Response(
        content=body,
        media_type=resume.mime,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_resume(
    resume_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
    s3: S3Client | None = Depends(get_s3_optional),
) -> None:
    resume = await _load_resume(session, resume_id=resume_id, user=user)

    deleted = await repo.delete_resume(session, resume_id=resume.id, user_id=user.id)
    if deleted is None:  # pragma: no cover — raced with another delete
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Resume not found")

    await repo.create_audit_log(
        session,
        user_id=str(user.id),
        action="resume_delete",
        resource_type="resume",
        resource_id=str(resume.id),
        ip_address=_client_ip(request),
    )
    await session.commit()

    # Best-effort blob cleanup after the DB commit: an orphaned S3 object
    # is preferable to a DB row pointing at a deleted blob.
    if s3 is not None:
        try:
            await s3.delete_object(deleted.blob_key)
        except Exception:  # pragma: no cover — cleanup only
            log.warning("resume delete: S3 cleanup failed for %s", deleted.blob_key)

    return None
