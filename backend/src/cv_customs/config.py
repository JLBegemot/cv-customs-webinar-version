"""Typed application settings loaded via pydantic-settings.

Webinar edition: auth, file upload and user-settings surfaces, local-only runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Walk up from this file looking for the repo-root ``.env`` file. Hard-coding
# ``parents[N]`` is brittle: the walk stops at the first directory that
# contains ``.env``; if none is found we fall back to the repo-root guess so
# pydantic-settings still emits a deterministic path in error messages.
def _locate_env_file() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        env = candidate / ".env"
        if env.is_file():
            return env
        # Stop once we leave the repo — ``pyproject.toml`` marks the root.
        if (candidate / "pyproject.toml").is_file():
            return env
    return here.parents[3] / ".env"


_ENV_FILE = _locate_env_file()


class Settings(BaseSettings):
    """All runtime configuration for the CV Customs service.

    Every field is validated at construction time. Call sites should prefer
    ``from cv_customs.config import settings`` and read attributes from the
    singleton. Module-level aliases below exist for backwards compatibility.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- environment --------------------------------------------------
    environment: Literal["dev", "staging", "prod", "test"] = Field(
        default="dev", alias="ENVIRONMENT"
    )

    # ---- JWT ----------------------------------------------------------
    jwt_secret: str = Field(default="", alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_expire_hours: int = Field(default=4, alias="JWT_EXPIRE_HOURS")

    # ---- HTTP ---------------------------------------------------------
    cors_allowed_origins_raw: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        alias="CORS_ALLOWED_ORIGINS",
    )

    # ---- PostgreSQL ---------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5433/cv_customs",
        alias="DATABASE_URL",
    )

    # ---- S3 / MinIO ---------------------------------------------------
    # Leave endpoint URL empty to use AWS S3; set to MinIO in dev.
    s3_endpoint_url: str = Field(
        default="http://localhost:9000", alias="S3_ENDPOINT_URL"
    )
    s3_access_key: str = Field(default="minioadmin", alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(default="minioadmin", alias="S3_SECRET_KEY")
    s3_bucket: str = Field(default="cv-customs-dev", alias="S3_BUCKET")
    s3_region: str = Field(default="us-east-1", alias="S3_REGION")

    # ---- Data retention ----------------------------------------------
    data_retention_soft_deleted_days: int = Field(
        default=30, alias="DATA_RETENTION_SOFT_DELETED_DAYS"
    )
    data_retention_inactive_account_days: int = Field(
        default=365, alias="DATA_RETENTION_INACTIVE_ACCOUNT_DAYS"
    )

    # ---- App base URL (used in password-reset emails) -----------------
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")

    # ---- Email / Yandex Cloud Postbox ---------------------------------
    # Postbox speaks the AWS SES v2 API. With no keys configured the email
    # client runs in console/log mode — fine for local-only use.
    yc_postbox_access_key_id: str = Field(default="", alias="YC_POSTBOX_ACCESS_KEY_ID")
    yc_postbox_secret_access_key: str = Field(
        default="", alias="YC_POSTBOX_SECRET_ACCESS_KEY"
    )
    yc_postbox_region: str = Field(default="ru-central1", alias="YC_POSTBOX_REGION")
    yc_postbox_endpoint_url: str = Field(
        default="https://postbox.cloud.yandex.net",
        alias="YC_POSTBOX_ENDPOINT_URL",
    )
    from_email: str = Field(default="no-reply@cv-customs.local", alias="FROM_EMAIL")
    from_name: str = Field(default="CV Customs", alias="FROM_NAME")

    # ---- Password reset ------------------------------------------------
    password_reset_token_ttl_minutes: int = Field(
        default=30, alias="PASSWORD_RESET_TOKEN_TTL_MINUTES"
    )

    # ---- Feature flags -------------------------------------------------
    feature_mfa: bool = Field(default=False, alias="FEATURE_MFA")

    # ---- Resume upload limits ------------------------------------------
    # Hard cap on the multipart body for ``POST /api/v1/resumes/upload``.
    resume_upload_max_bytes: int = Field(
        default=5 * 1024 * 1024, alias="RESUME_UPLOAD_MAX_BYTES"
    )

    # ---- derived ------------------------------------------------------
    @property
    def cors_allowed_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_allowed_origins_raw.split(",")
            if origin.strip()
        ]

    # ---- validators ---------------------------------------------------
    @field_validator("jwt_secret")
    @classmethod
    def _validate_jwt_secret(cls, value: str) -> str:
        # Same "at least 32 chars" rule for every environment. Tests use the
        # conftest-provided dummy secret, which satisfies this check.
        if not value or value == "change-me-in-production":
            raise ValueError(
                "JWT_SECRET is not configured. "
                "Set a random string of at least 32 characters in the environment."
            )
        if len(value) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters long.")
        return value


# ---------------------------------------------------------------------------
# Singleton + backwards-compatible module-level aliases
# ---------------------------------------------------------------------------

settings = Settings()  # type: ignore[call-arg]

# Legacy constants — kept so existing imports like
# `from ..config import JWT_SECRET` continue to work. New code should use
# `from .config import settings` and read attributes directly.
JWT_SECRET: str = settings.jwt_secret
JWT_ALGORITHM: str = settings.jwt_algorithm
JWT_EXPIRE_HOURS: int = settings.jwt_expire_hours

CORS_ALLOWED_ORIGINS: list[str] = settings.cors_allowed_origins

DATABASE_URL: str = settings.database_url

S3_ENDPOINT_URL: str = settings.s3_endpoint_url
S3_ACCESS_KEY: str = settings.s3_access_key
S3_SECRET_KEY: str = settings.s3_secret_key
S3_BUCKET: str = settings.s3_bucket
S3_REGION: str = settings.s3_region

DATA_RETENTION_SOFT_DELETED_DAYS: int = settings.data_retention_soft_deleted_days
DATA_RETENTION_INACTIVE_ACCOUNT_DAYS: int = (
    settings.data_retention_inactive_account_days
)

ENVIRONMENT: str = settings.environment
