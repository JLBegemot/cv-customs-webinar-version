"""Контракт регистрации, входа и выданного JWT.

Покрываются обе поверхности — легаси ``/api/auth/*`` и ``/api/v1/auth/*``
(``auth_v1`` переиспользует те же хендлеры, поэтому подробные ветки
гоняются один раз на легаси-префиксе, а на v1 проверяется только то, что
алиас ведёт в тот же обработчик).

Ветки регистрации в порядке, в котором их проверяет хендлер: нет согласия
(152-ФЗ) → 422, занятый email → 409, короткий пароль → 422. Порядок важен:
для занятого email с коротким паролем контракт обещает именно 409.

Отдельный блок — ``get_current_user``: истёкший, чужой подписью подписанный
и осиротевший (пользователь удалён) токен обязаны давать 401, иначе любой
из них становится ключом от аккаунта.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
from jose import jwt
from sqlalchemy import select

from cv_customs.config import JWT_ALGORITHM, JWT_EXPIRE_HOURS, JWT_SECRET
from cv_customs.db import repositories as repo
from cv_customs.db.models import AuditLog, User


@pytest.fixture
def client(build_client):
    return build_client()


def _register_payload(**overrides) -> dict:
    payload = {
        "email": "newcomer@example.com",
        "password": "correct-horse",
        "consent": True,
    }
    payload.update(overrides)
    return payload


async def _audit_actions(session_factory, action: str) -> list[AuditLog]:
    async with session_factory() as session:
        rows = await session.execute(select(AuditLog).where(AuditLog.action == action))
        return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# POST /api/auth/register — happy path
# ---------------------------------------------------------------------------


async def test_register_returns_usable_token_for_active_account(
    client, session_factory
):
    """В упрощённой редакции нет подтверждения почты — аккаунт живой сразу,
    поэтому выданный токен обязан открывать закрытую ручку без доп. шагов."""

    resp = client.post("/api/auth/register", json=_register_payload())
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["email"] == "newcomer@example.com"
    assert body["user"]["consent_given_at"] is not None

    me = client.get(
        "/api/user/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["id"] == body["user"]["id"]


async def test_register_normalises_email_to_lower_case(client, session_factory):
    resp = client.post(
        "/api/auth/register", json=_register_payload(email="Mixed.Case@Example.COM")
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == "mixed.case@example.com"

    async with session_factory() as session:
        user = await repo.get_user_by_email(session, "mixed.case@example.com")
    assert user is not None, "email должен храниться в нижнем регистре"


async def test_register_stores_bcrypt_hash_not_plaintext(client, session_factory):
    client.post("/api/auth/register", json=_register_payload(password="s3cret-pw"))

    async with session_factory() as session:
        user = await repo.get_user_by_email_ci(session, "newcomer@example.com")
    assert user is not None
    assert user.password_hash != "s3cret-pw"
    assert bcrypt.checkpw(b"s3cret-pw", user.password_hash.encode())
    # Хендлер штампует password_updated_at — на него опираются ротации пароля.
    assert user.password_updated_at is not None


async def test_register_records_cross_border_consent_only_when_given(
    client, session_factory
):
    default = client.post("/api/auth/register", json=_register_payload())
    assert default.json()["user"]["cross_border_consent_at"] is None

    opted_in = client.post(
        "/api/auth/register",
        json=_register_payload(email="cb@example.com", cross_border_consent=True),
    )
    assert opted_in.status_code == 200, opted_in.text
    assert opted_in.json()["user"]["cross_border_consent_at"] is not None


async def test_register_writes_audit_log_with_forwarded_ip(client, session_factory):
    resp = client.post(
        "/api/auth/register",
        json=_register_payload(),
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    assert resp.status_code == 200, resp.text

    rows = await _audit_actions(session_factory, "register")
    assert len(rows) == 1
    assert rows[0].ip_address == "203.0.113.7"
    assert rows[0].resource_id == resp.json()["user"]["id"]


# ---------------------------------------------------------------------------
# POST /api/auth/register — отказы
# ---------------------------------------------------------------------------


async def test_register_without_consent_is_rejected_and_creates_nothing(
    client, session_factory
):
    resp = client.post("/api/auth/register", json=_register_payload(consent=False))
    assert resp.status_code == 422
    assert "152-FZ" in resp.json()["detail"]

    async with session_factory() as session:
        assert await repo.get_user_by_email_ci(session, "newcomer@example.com") is None


async def test_register_rejects_short_password_and_creates_nothing(
    client, session_factory
):
    resp = client.post("/api/auth/register", json=_register_payload(password="12345"))
    assert resp.status_code == 422
    assert "6 characters" in resp.json()["detail"]

    async with session_factory() as session:
        assert await repo.get_user_by_email_ci(session, "newcomer@example.com") is None


async def test_register_rejects_duplicate_email_ignoring_case(client, session_factory):
    first = client.post("/api/auth/register", json=_register_payload())
    assert first.status_code == 200, first.text

    dup = client.post(
        "/api/auth/register", json=_register_payload(email="NEWCOMER@example.com")
    )
    assert dup.status_code == 409, dup.text

    async with session_factory() as session:
        users = await session.execute(select(User.id))
    assert len(list(users.scalars().all())) == 1


async def test_register_rejects_malformed_email(client):
    resp = client.post("/api/auth/register", json=_register_payload(email="not-email"))
    # Ловится схемой (EmailStr) — до тела хендлера запрос не доходит.
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


async def test_login_issues_token_and_audits(client, make_user, session_factory):
    await make_user("jane@example.com", password="right-password")

    resp = client.post(
        "/api/auth/login",
        json={"email": "jane@example.com", "password": "right-password"},
        headers={"X-Forwarded-For": "198.51.100.4"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == "jane@example.com"

    me = client.get(
        "/api/user/me",
        headers={"Authorization": f"Bearer {resp.json()['access_token']}"},
    )
    assert me.status_code == 200, me.text

    rows = await _audit_actions(session_factory, "login")
    assert len(rows) == 1
    assert rows[0].ip_address == "198.51.100.4"


async def test_login_matches_email_case_insensitively(client, make_user):
    await make_user("jane@example.com", password="right-password")

    resp = client.post(
        "/api/auth/login",
        json={"email": "JANE@Example.com", "password": "right-password"},
    )
    assert resp.status_code == 200, resp.text


async def test_login_rejects_wrong_password_without_auditing(
    client, make_user, session_factory
):
    await make_user("jane@example.com", password="right-password")

    resp = client.post(
        "/api/auth/login",
        json={"email": "jane@example.com", "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"
    assert await _audit_actions(session_factory, "login") == []


async def test_login_rejects_unknown_email_with_same_message(client, make_user):
    """Формулировка та же, что и при неверном пароле — иначе ручка
    превращается в оракул «зарегистрирован ли такой email»."""

    await make_user("jane@example.com", password="right-password")

    resp = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "right-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"


async def test_login_rejects_account_without_password_hash(client, make_user):
    # Аккаунт без пароля (заведён не через регистрацию) не должен пускать
    # по пустой строке или любому другому вводу.
    await make_user("nopass@example.com", password=None)

    resp = client.post(
        "/api/auth/login", json={"email": "nopass@example.com", "password": ""}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /api/v1/auth/* — алиасы и logout
# ---------------------------------------------------------------------------


async def test_v1_register_and_login_mirror_legacy_routes(client):
    registered = client.post("/api/v1/auth/register", json=_register_payload())
    assert registered.status_code == 200, registered.text

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"email": "newcomer@example.com", "password": "correct-horse"},
    )
    assert logged_in.status_code == 200, logged_in.text
    assert logged_in.json()["user"]["id"] == registered.json()["user"]["id"]


async def test_logout_is_a_no_op_204(client, make_user, auth_headers):
    """Серверного блоклиста в упрощённой редакции нет: ручка отвечает 204,
    а токен намеренно остаётся рабочим — его выбрасывает клиент."""

    user_id = await make_user("bye@example.com")
    headers = auth_headers(user_id)

    resp = client.post("/api/v1/auth/logout", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""

    still_valid = client.get("/api/user/me", headers=headers)
    assert still_valid.status_code == 200, still_valid.text


async def test_logout_requires_a_bearer_header(client):
    assert client.post("/api/v1/auth/logout").status_code == 401


# ---------------------------------------------------------------------------
# JWT / get_current_user
# ---------------------------------------------------------------------------


def test_access_token_lifetime_matches_config():
    from cv_customs.api.auth import create_access_token

    claims = jwt.get_unverified_claims(create_access_token(uuid.uuid4()))
    assert claims["exp"] - claims["iat"] == JWT_EXPIRE_HOURS * 3600


async def test_missing_authorization_header_is_rejected(client):
    assert client.get("/api/user/me").status_code == 401


async def test_malformed_token_is_rejected(client):
    resp = client.get(
        "/api/user/me", headers={"Authorization": "Bearer not-a-jwt-at-all"}
    )
    assert resp.status_code == 401


async def test_expired_token_is_rejected(client, make_user):
    user_id = await make_user("stale@example.com")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    expired = jwt.encode(
        {"sub": str(user_id), "iat": past - timedelta(hours=1), "exp": past},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    resp = client.get("/api/user/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


async def test_token_signed_with_another_secret_is_rejected(client, make_user):
    user_id = await make_user("forged@example.com")
    forged = jwt.encode(
        {
            "sub": str(user_id),
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "an-attacker-secret-of-at-least-32-characters",
        algorithm=JWT_ALGORITHM,
    )

    resp = client.get("/api/user/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


async def test_token_of_deleted_user_is_rejected(
    client, make_user, auth_headers, session_factory
):
    user_id = await make_user("ghost@example.com")
    headers = auth_headers(user_id)
    assert client.get("/api/user/me", headers=headers).status_code == 200

    async with session_factory() as session:
        await repo.hard_delete_user(session, user_id)
        await session.commit()

    assert client.get("/api/user/me", headers=headers).status_code == 401
