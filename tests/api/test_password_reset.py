"""Контракт сброса пароля: ``/api/v1/auth/password/reset/{request,confirm}``.

Ключевые свойства, которые здесь проверяются:

* ``/request`` всегда отвечает 202 и по неизвестному адресу **не отправляет
  ничего** — иначе ручка становится оракулом «зарегистрирован ли email»;
* токен одноразовый и с TTL: повторное и просроченное предъявление — 400;
* ``/confirm`` реально ротирует пароль (старый перестаёт пускать) и выдаёт
  свежую сессию.

Токены живут в процессе (``auth_reset._TOKENS``), внешних сервисов нет;
почта подменяется ``NullEmailClient`` из стенда, и сам факт отправки
проверяется в ``email_client.sent``, а не по статус-коду.
"""

from __future__ import annotations

import bcrypt
import pytest

from cv_customs.db import repositories as repo


@pytest.fixture
async def reset_ctx(build_client, session_factory, email_client, make_user):
    user_id = await make_user("jane@example.com", password="old-password")

    return {
        "client": build_client(email=email_client),
        "email": email_client,
        "user_id": user_id,
        "SessionLocal": session_factory,
    }


def _request_reset(ctx, email: str = "jane@example.com"):
    return ctx["client"].post(
        "/api/v1/auth/password/reset/request", json={"email": email}
    )


def _token_from_last_email(ctx) -> str:
    return ctx["email"].sent[-1]["model"]["reset_url"].rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# /request
# ---------------------------------------------------------------------------


async def test_reset_request_is_202_for_unknown_email_and_sends_nothing(reset_ctx):
    resp = _request_reset(reset_ctx, "nobody@example.com")
    assert resp.status_code == 202, resp.text
    # 202 сам по себе ничего не говорит — важно, что письма не было.
    assert reset_ctx["email"].sent == []


async def test_reset_request_dispatches_email_and_stores_token(reset_ctx):
    resp = _request_reset(reset_ctx)
    assert resp.status_code == 202, resp.text

    assert reset_ctx["email"].sent, "reset email was not queued"
    record = reset_ctx["email"].sent[-1]
    assert record["template_alias"] == "password-reset"
    assert record["to"] == "jane@example.com"
    assert "reset_url" in record["model"]
    assert record["model"]["expires_in_minutes"] > 0


async def test_reset_request_matches_email_case_insensitively(reset_ctx):
    resp = _request_reset(reset_ctx, "JANE@Example.COM")
    assert resp.status_code == 202, resp.text
    assert reset_ctx["email"].sent[-1]["to"] == "jane@example.com"


# ---------------------------------------------------------------------------
# /confirm
# ---------------------------------------------------------------------------


async def test_reset_confirm_rotates_password_and_consumes_token(reset_ctx):
    _request_reset(reset_ctx)
    token = _token_from_last_email(reset_ctx)

    resp = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm?email=jane@example.com",
        json={"token": token, "new_password": "brand-new-password"},
    )
    assert resp.status_code == 200, resp.text
    assert "access_token" in resp.json()

    async with reset_ctx["SessionLocal"]() as session:
        user = await repo.get_user_by_email_ci(session, "jane@example.com")
    assert user is not None
    assert bcrypt.checkpw(b"brand-new-password", user.password_hash.encode())

    # Токены одноразовые — второе предъявление того же токена отбивается.
    replay = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm?email=jane@example.com",
        json={"token": token, "new_password": "another-password"},
    )
    assert replay.status_code == 400


async def test_old_password_stops_working_after_reset(reset_ctx):
    _request_reset(reset_ctx)
    reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm?email=jane@example.com",
        json={"token": _token_from_last_email(reset_ctx), "new_password": "new-pw-123"},
    )

    stale = reset_ctx["client"].post(
        "/api/auth/login",
        json={"email": "jane@example.com", "password": "old-password"},
    )
    assert stale.status_code == 401, stale.text

    fresh = reset_ctx["client"].post(
        "/api/auth/login", json={"email": "jane@example.com", "password": "new-pw-123"}
    )
    assert fresh.status_code == 200, fresh.text


async def test_reset_confirm_rejects_expired_token(reset_ctx, monkeypatch):
    """TTL считается в момент выпуска, поэтому отрицательный срок жизни даёт
    заведомо просроченный токен без ожидания в тесте (FORBID-002)."""

    from cv_customs.config import settings

    monkeypatch.setattr(settings, "password_reset_token_ttl_minutes", -1)
    _request_reset(reset_ctx)

    resp = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm?email=jane@example.com",
        json={"token": _token_from_last_email(reset_ctx), "new_password": "whatever-1"},
    )
    assert resp.status_code == 400, resp.text

    # Пароль остался прежним.
    async with reset_ctx["SessionLocal"]() as session:
        user = await repo.get_user_by_email_ci(session, "jane@example.com")
    assert bcrypt.checkpw(b"old-password", user.password_hash.encode())


async def test_reset_confirm_rejects_unknown_token(reset_ctx):
    resp = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm?email=jane@example.com",
        json={"token": "definitely-not-a-real-token", "new_password": "whatever-1"},
    )
    assert resp.status_code == 400


async def test_reset_confirm_hides_whether_the_account_exists(reset_ctx):
    """Неизвестный адрес и неверный токен обязаны отвечать одинаково —
    иначе ручка снова превращается в перечислитель аккаунтов."""

    unknown = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm?email=nobody@example.com",
        json={"token": "x" * 32, "new_password": "whatever-1"},
    )
    assert unknown.status_code == 400
    assert "Invalid reset token" in unknown.json()["detail"]


async def test_reset_confirm_requires_email(reset_ctx):
    # Токен должен быть ≥16 символов, чтобы пройти валидацию схемы: нужна
    # ветка хендлера «нет email → 400», а не 422 на теле запроса.
    resp = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": "x" * 16, "new_password": "whatever-1"},
    )
    assert resp.status_code == 400
    assert "account email" in resp.json()["detail"]


async def test_reset_confirm_accepts_email_from_header(reset_ctx):
    """Клиент может передать адрес заголовком ``X-Reset-Email`` вместо
    query-параметра — обе формы описаны в контракте хендлера."""

    _request_reset(reset_ctx)

    resp = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": _token_from_last_email(reset_ctx), "new_password": "hdr-pw-123"},
        headers={"X-Reset-Email": "jane@example.com"},
    )
    assert resp.status_code == 200, resp.text


async def test_reset_confirm_rejects_short_new_password(reset_ctx):
    _request_reset(reset_ctx)

    resp = reset_ctx["client"].post(
        "/api/v1/auth/password/reset/confirm?email=jane@example.com",
        json={"token": _token_from_last_email(reset_ctx), "new_password": "12345"},
    )
    # Отбивается схемой (min_length=6) — до ротации пароля дело не доходит.
    assert resp.status_code == 422
