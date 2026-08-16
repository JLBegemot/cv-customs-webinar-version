"""Small request helpers shared across routers."""

from __future__ import annotations

from fastapi import Request


def client_ip(request: Request) -> str:
    """Best-effort client IP extraction.

    Honours ``X-Forwarded-For`` (first hop) when present — a reverse proxy
    sets it. Falls back to the ASGI peer address.
    """

    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
