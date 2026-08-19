"""FastAPI app: REST API for auth, file upload and user settings.

Webinar edition — a local-only, plain REST API over Postgres + S3.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import CORS_ALLOWED_ORIGINS
from .db.session import engine
from .infra.email import create_email_client
from .infra.logging import RequestIdMiddleware, configure_logging
from .infra.storage import create_s3_client
from .api.auth import router as auth_router
from .api.auth_v1 import router as auth_v1_router
from .api.auth_reset import router as auth_reset_router
from .api import auth_mfa as _auth_mfa
from .api.legal import router as legal_router
from .api.resumes import router as resumes_router
from .api.user import router as user_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: configure logging, prepare the S3 and email clients.

    Schema management is fully delegated to Alembic — run
    ``alembic upgrade head`` before booting the API.
    """

    configure_logging()
    app.state.s3 = create_s3_client()
    app.state.email = create_email_client()

    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(
    title="CV Customs (webinar edition)",
    description="CV storage service: auth, file upload, user settings",
    lifespan=lifespan,
)

app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(
    auth_router
)  # /api/auth/* — email/password (the only sign-in method)
app.include_router(auth_v1_router)  # /api/v1/auth/{register,login,logout}
app.include_router(auth_reset_router)  # /api/v1/auth/password/reset/{request,confirm}
_auth_mfa.mount_if_enabled(app)  # /api/v1/auth/mfa/* — gated by FEATURE_MFA
app.include_router(legal_router)  # /api/legal/*
app.include_router(user_router)  # /api/user/* — profile, consent, account deletion
app.include_router(resumes_router)  # /api/v1/resumes — uploaded files (metadata + blob)


@app.get("/health", tags=["infra"], include_in_schema=False)
async def healthcheck() -> dict[str, object]:
    return {"status": "ok", "version": 1}
