"""Structured logging setup.

``configure_logging`` wires structlog into the stdlib logging tree so both
stdlib loggers (FastAPI, SQLAlchemy) and structlog loggers render
through the same pipeline: human-readable in ``dev`` / ``test`` and JSON in
``staging`` / ``prod``. A small Starlette middleware attaches a per-request
``request_id`` to the contextvars context so every log line during the
request inherits it.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import settings


def configure_logging() -> None:
    """Install structlog processors + a stdlib handler writing to stderr."""

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.environment in {"dev", "test"}:
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer(
            colors=settings.environment == "dev"
        )
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    root = logging.getLogger()
    # Replace handlers so re-invocation (e.g. in tests) stays idempotent.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    root.setLevel(logging.INFO if settings.environment != "dev" else logging.DEBUG)

    # Tame noisy libraries.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)


class RequestIdMiddleware:
    """Attach a ``request_id`` to the structlog context for every request.

    Accepts an inbound ``X-Request-Id`` header if present (useful when
    requests come through a load balancer that already tags them) and
    echoes it back in the response.
    """

    HEADER_NAME = b"x-request-id"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(self.HEADER_NAME)
        request_id = incoming.decode("latin-1") if incoming else uuid.uuid4().hex

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_header(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers") or [])
                raw_headers.append((self.HEADER_NAME, request_id.encode("latin-1")))
                message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.clear_contextvars()
