"""Контракт типизированных настроек (``config.Settings``).

Проверяются **валидаторы и парсинг**, а не значения синглтона `settings`:
синглтон собирается в том числе из корневого `.env`, поэтому ассерт на его
поля проверял бы локальный конфиг разработчика, а не код. Все тесты ниже
строят свой ``Settings`` с ``_env_file=None`` и явно заданным окружением —
результат одинаков на любой машине и в CI.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cv_customs.config import Settings

_VALID_SECRET = "test-secret-key-must-be-at-least-32-chars-long"


def _build(monkeypatch, **env) -> Settings:
    """``Settings`` из явного окружения, в обход `.env`."""

    monkeypatch.setenv("JWT_SECRET", _VALID_SECRET)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def test_jwt_secret_too_short_is_rejected(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "32" in str(excinfo.value)


def test_jwt_secret_placeholder_is_rejected(monkeypatch):
    """Дефолт из `.env.example` не должен уехать в рантайм как рабочий ключ."""

    monkeypatch.setenv("JWT_SECRET", "change-me-in-production")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "JWT_SECRET" in str(excinfo.value)


def test_jwt_secret_of_exactly_32_chars_is_accepted(monkeypatch):
    # Граница правила «не короче 32» — 32 символа проходят.
    secret = "x" * 32
    monkeypatch.setenv("JWT_SECRET", secret)
    assert Settings(_env_file=None).jwt_secret == secret  # type: ignore[call-arg]


def test_jwt_expire_hours_parsed_as_int(monkeypatch):
    assert _build(monkeypatch, JWT_EXPIRE_HOURS="12").jwt_expire_hours == 12


# ---------------------------------------------------------------------------
# Environment / типы
# ---------------------------------------------------------------------------


def test_environment_must_be_enum_value(monkeypatch):
    # "production" не входит в {dev, staging, prod, test}.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("JWT_SECRET", _VALID_SECRET)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_boolean_like_envvar_parses(monkeypatch):
    assert _build(monkeypatch, FEATURE_MFA="true").feature_mfa is True
    assert _build(monkeypatch, FEATURE_MFA="0").feature_mfa is False


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_origins_split_and_stripped(monkeypatch):
    settings = _build(
        monkeypatch,
        CORS_ALLOWED_ORIGINS="http://a.example , http://b.example,, ",
    )
    assert settings.cors_allowed_origins == ["http://a.example", "http://b.example"]


# ---------------------------------------------------------------------------
# Умолчания
# ---------------------------------------------------------------------------


def test_s3_defaults_point_to_local_compose(monkeypatch):
    """Умолчания в коде обязаны совпадать с docker-compose.yml.

    Переменные окружения снимаются явно, иначе тест проверял бы `.env`
    разработчика, а не дефолты из ``config.py``.
    """

    for key in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_REGION"):
        monkeypatch.delenv(key, raising=False)
    settings = _build(monkeypatch)

    # docker-compose.yml пробрасывает MinIO S3 API на 9000.
    assert settings.s3_endpoint_url == "http://localhost:9000"
    assert settings.s3_bucket == "cv-customs-dev"
