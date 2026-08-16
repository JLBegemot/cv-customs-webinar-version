"""DB package re-exports.

Tests and callers import model classes, the repository module, and the
session factory from the package root so the layout of individual
submodules can evolve without a cross-repo rename. Everything below is a
thin re-export of a symbol already defined in a sibling module — no logic
lives here.
"""

from __future__ import annotations

from .. import config
from . import repositories
from .models import AuditLog, Base, Resume, User
from .session import async_session, engine, get_session

__all__ = [
    # SQLAlchemy base + session helpers
    "Base",
    "async_session",
    "engine",
    "get_session",
    # Model classes
    "AuditLog",
    "Resume",
    "User",
    # Sibling modules
    "config",
    "repositories",
]
