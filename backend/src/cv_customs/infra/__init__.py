"""Infrastructure adapters (Redis, object storage, logging, email).

Introduced in Week 1 of the Phase-1 MVP plan. Each module exposes a small
helper that wires its client into ``app.state`` during FastAPI ``lifespan``
startup and a FastAPI dependency that pulls it back out.

Callers import adapter classes directly from this package
(``from cv_customs.infra import NullEmailClient``) so the individual submodule
layout can evolve without touching imports across the codebase.
"""

from __future__ import annotations

from .email import (
    EmailClient,
    NullEmailClient,
    YandexPostboxEmailClient,
    create_email_client,
)

__all__ = [
    "EmailClient",
    "NullEmailClient",
    "YandexPostboxEmailClient",
    "create_email_client",
]
