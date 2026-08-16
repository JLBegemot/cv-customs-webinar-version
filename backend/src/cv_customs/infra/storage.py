"""S3-compatible object storage adapter (MinIO in dev, AWS S3 in prod).

Used from Week 3 onwards to persist original resume blobs and rendered PDF
exports. The client is created lazily on first use and cached on
``app.state.s3``; concrete calls go through the small helpers below so the
rest of the code never has to think about aioboto3 session lifetimes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import Request

from ..config import settings

if TYPE_CHECKING:
    import aioboto3

log = logging.getLogger(__name__)


@dataclass
class S3Client:
    """Wrapper around an ``aioboto3`` session + configuration.

    aioboto3 clients themselves are async context managers, so we hold the
    *session* and open a fresh client per call. This is the recommended
    pattern per aioboto3 README — sessions are cheap, clients should be
    short-lived.
    """

    session: "aioboto3.Session"
    endpoint_url: str | None
    region: str
    bucket: str
    access_key: str
    secret_key: str

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "region_name": self.region,
            "aws_access_key_id": self.access_key,
            "aws_secret_access_key": self.secret_key,
        }
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        return kwargs

    def _client_cm(self):
        return self.session.client("s3", **self._client_kwargs())

    async def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str | None = None,
    ) -> str:
        """Upload bytes under ``{bucket}/{key}`` and return the object key."""

        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        async with self._client_cm() as client:
            await client.put_object(
                Bucket=self.bucket, Key=key, Body=body, **extra
            )
        return key

    async def get_object(self, key: str) -> bytes:
        """Download ``{bucket}/{key}`` fully into memory.

        Uploads are capped at a few megabytes, so buffering the whole blob
        is fine — no need for streaming plumbing here.
        """

        async with self._client_cm() as client:
            response = await client.get_object(Bucket=self.bucket, Key=key)
            async with response["Body"] as stream:
                return await stream.read()

    async def delete_object(self, key: str) -> None:
        """Delete ``{bucket}/{key}``. Missing keys are a no-op per S3."""

        async with self._client_cm() as client:
            await client.delete_object(Bucket=self.bucket, Key=key)

    async def get_presigned_url(
        self, key: str, expires_in_seconds: int = 300
    ) -> str:
        """Return a time-limited download URL for ``{bucket}/{key}``."""

        async with self._client_cm() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in_seconds,
            )

    async def head_bucket(self) -> bool:
        """Return True if the configured bucket is reachable and readable."""

        async with self._client_cm() as client:
            try:
                await client.head_bucket(Bucket=self.bucket)
                return True
            except Exception:
                return False


def create_s3_client() -> S3Client:
    """Build the singleton ``S3Client`` from settings.

    Kept synchronous — the underlying aioboto3.Session is cheap and does
    not open any sockets until a client context is entered.
    """

    import aioboto3

    session = aioboto3.Session()
    return S3Client(
        session=session,
        endpoint_url=settings.s3_endpoint_url or None,
        region=settings.s3_region,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )


def get_s3(request: Request) -> S3Client:
    """FastAPI dependency returning the shared ``S3Client``.

    Raises if S3 is unconfigured — use :func:`get_s3_optional` on paths
    that should degrade gracefully (e.g. the Week-3 resume upload which
    still succeeds if the blob can't be stored).
    """

    client: S3Client | None = getattr(request.app.state, "s3", None)
    if client is None:
        raise RuntimeError(
            "S3 client is not initialised. "
            "Did the FastAPI lifespan run (see cv_customs.main)?"
        )
    return client


def get_s3_optional(request: Request) -> S3Client | None:
    """Return the shared S3 client or ``None`` when unconfigured.

    Unit tests explicitly set ``app.state.s3 = None`` so the resume
    upload path needs a dependency that tolerates that — otherwise the
    test suite fails before the handler runs.
    """

    return getattr(request.app.state, "s3", None)


async def ping_s3(client: S3Client | None) -> bool:
    """Return True if the bucket is reachable. Safe for /health."""

    if client is None:
        return False
    try:
        return await client.head_bucket()
    except Exception:
        return False
