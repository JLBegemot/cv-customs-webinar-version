"""Контракт аккаунта: ``/api/user/{me,personal-data,revoke-consent,account}``.

Три требования 152-ФЗ, которые эти ручки закрывают, и что здесь про них
проверяется:

* ст. 14 (право на доступ) — ``GET /personal-data`` отдаёт **все** данные
  субъекта, включая загруженные файлы, а не только строку users;
* ст. 9 п. 2 (отзыв согласия) и ст. 21 (право на удаление) — обе ручки
  физически сносят пользователя вместе с его резюме (каскад по FK), а не
  помечают его удалённым;
* PP РФ №1119 — каждое из этих действий пишет строку в audit_log, и IP в
  ней берётся из ``X-Forwarded-For``, а не у пира ASGI: за обратным прокси
  ``request.client.host`` — это адрес прокси, то есть запись в журнале
  бесполезна.

Обе разрушающие ручки требуют явного ``confirm=true`` — тесты проверяют,
что без него не только приходит 422, но и ничего не удаляется.

``PATCH /api/user/me`` (частичное обновление профиля) покрыт отдельным
блоком: смена email применяется сразу и меняет идентичность входа,
занятость проверяется без учёта регистра, согласия идемпотентны, а отзыв
основного согласия через PATCH запрещён — для него есть /revoke-consent.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cv_customs.db import repositories as repo
from cv_customs.db.models import AuditLog, Resume


@pytest.fixture
async def account_ctx(build_client, session_factory, make_user, auth_headers):
    # Пароль не нужен: эти ручки открываются JWT, через логин в них не ходят.
    user_id = await make_user("alice@example.com", password=None)

    return {
        "client": build_client(),
        "user_id": user_id,
        "headers": auth_headers(user_id),
        "SessionLocal": session_factory,
    }


async def _seed_resume(session_factory, user_id, filename: str = "cv.pdf"):
    """Метаданные загруженного файла без похода в S3 — тестам этого блока
    нужен только факт наличия связанной строки."""

    async with session_factory() as session:
        resume = await repo.create_resume_file(
            session,
            user_id=user_id,
            filename=filename,
            mime="application/pdf",
            size_bytes=1024,
            blob_key=f"resumes/{uuid.uuid4()}/original.pdf",
        )
        await session.commit()
        return resume.id


async def _audit_rows(session_factory, *actions: str) -> list[AuditLog]:
    async with session_factory() as session:
        rows = await session.execute(
            select(AuditLog).where(AuditLog.action.in_(actions))
        )
        return list(rows.scalars().all())


async def _user_exists(session_factory, user_id) -> bool:
    async with session_factory() as session:
        return await repo.get_user_by_id(session, user_id) is not None


# ---------------------------------------------------------------------------
# GET /api/user/me
# ---------------------------------------------------------------------------


async def test_me_returns_profile_of_token_owner(account_ctx):
    resp = account_ctx["client"].get("/api/user/me", headers=account_ctx["headers"])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["id"] == str(account_ctx["user_id"])
    assert body["email"] == "alice@example.com"
    assert body["consent_given_at"] is not None
    assert body["created_at"]


async def test_me_requires_auth(account_ctx):
    assert account_ctx["client"].get("/api/user/me").status_code == 401


async def test_me_never_exposes_the_password_hash(account_ctx):
    resp = account_ctx["client"].get("/api/user/me", headers=account_ctx["headers"])
    assert "password_hash" not in resp.json()
    assert "mfa_secret" not in resp.json()


# ---------------------------------------------------------------------------
# PATCH /api/user/me — частичное обновление профиля
# ---------------------------------------------------------------------------


async def _db_user(session_factory, user_id):
    async with session_factory() as session:
        return await repo.get_user_by_id(session, user_id)


async def test_patch_me_changes_email_and_login_identity(
    build_client, make_user, auth_headers, session_factory
):
    user_id = await make_user("bob@example.com", password="pw")
    client = build_client()
    headers = auth_headers(user_id)

    resp = client.patch(
        "/api/user/me",
        headers={**headers, "X-Forwarded-For": "2.2.2.2"},
        json={"email": "new-bob@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "new-bob@example.com"

    # Смена применяется сразу: старый email больше не логинится, новый — да.
    old_login = client.post(
        "/api/v1/auth/login", json={"email": "bob@example.com", "password": "pw"}
    )
    assert old_login.status_code == 401, old_login.text
    new_login = client.post(
        "/api/v1/auth/login", json={"email": "new-bob@example.com", "password": "pw"}
    )
    assert new_login.status_code == 200, new_login.text

    rows = await _audit_rows(session_factory, "email_changed")
    assert len(rows) == 1
    assert rows[0].ip_address == "2.2.2.2"
    assert "bob@example.com -> new-bob@example.com" in rows[0].details


async def test_patch_me_rejects_taken_email_case_insensitive(account_ctx, make_user):
    await make_user("taken@example.com", password=None)

    resp = account_ctx["client"].patch(
        "/api/user/me",
        headers=account_ctx["headers"],
        json={"email": "TAKEN@example.com"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "EMAIL_TAKEN"

    user = await _db_user(account_ctx["SessionLocal"], account_ctx["user_id"])
    assert user.email == "alice@example.com"
    assert await _audit_rows(account_ctx["SessionLocal"], "email_changed") == []


async def test_patch_me_same_email_is_noop(account_ctx):
    resp = account_ctx["client"].patch(
        "/api/user/me",
        headers=account_ctx["headers"],
        json={"email": "ALICE@example.com"},
    )
    assert resp.status_code == 200, resp.text
    # Регистр не считается изменением: email остаётся как был, аудита нет.
    assert resp.json()["email"] == "alice@example.com"
    assert await _audit_rows(account_ctx["SessionLocal"], "email_changed") == []


async def test_patch_me_rejects_malformed_email(account_ctx):
    resp = account_ctx["client"].patch(
        "/api/user/me",
        headers=account_ctx["headers"],
        json={"email": "not-an-email"},
    )
    assert resp.status_code == 422, resp.text


async def test_patch_me_grants_consent_once(
    build_client, make_user, auth_headers, session_factory
):
    user_id = await make_user("fresh@example.com", password=None, consent=False)
    client = build_client()
    headers = auth_headers(user_id)

    first = client.patch("/api/user/me", headers=headers, json={"consent": True})
    assert first.status_code == 200, first.text
    granted_at = first.json()["consent_given_at"]
    assert granted_at is not None

    user = await _db_user(session_factory, user_id)
    assert user.consent_given_at is not None

    # Канонический вид таймстемпа — из перечитанного профиля: SQLite не
    # хранит таймзону, поэтому строка первого ответа (свежий tz-aware
    # datetime) и строка после перечитывания из БД отличаются суффиксом
    # ``+00:00``. Сверяем значения одного происхождения.
    stored_at = client.get("/api/user/me", headers=headers).json()["consent_given_at"]

    # Повторная выдача идемпотентна: таймстемп не сдвигается, аудит один.
    second = client.patch("/api/user/me", headers=headers, json={"consent": True})
    assert second.status_code == 200, second.text
    assert second.json()["consent_given_at"] == stored_at

    rows = await _audit_rows(session_factory, "consent_granted")
    assert len(rows) == 1


async def test_patch_me_cross_border_grant_and_revoke(account_ctx):
    client, headers = account_ctx["client"], account_ctx["headers"]

    grant = client.patch(
        "/api/user/me", headers=headers, json={"cross_border_consent": True}
    )
    assert grant.status_code == 200, grant.text
    assert grant.json()["cross_border_consent_at"] is not None
    # См. комментарий про SQLite и таймзону в тесте выше: канонический
    # таймстемп берётся перечитыванием профиля, а не из ответа grant.
    granted_at = client.get("/api/user/me", headers=headers).json()[
        "cross_border_consent_at"
    ]

    again = client.patch(
        "/api/user/me", headers=headers, json={"cross_border_consent": True}
    )
    assert again.json()["cross_border_consent_at"] == granted_at

    revoke = client.patch(
        "/api/user/me", headers=headers, json={"cross_border_consent": False}
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["cross_border_consent_at"] is None

    user = await _db_user(account_ctx["SessionLocal"], account_ctx["user_id"])
    assert user.cross_border_consent_at is None

    granted_rows = await _audit_rows(
        account_ctx["SessionLocal"], "cross_border_consent_granted"
    )
    revoked_rows = await _audit_rows(
        account_ctx["SessionLocal"], "cross_border_consent_revoked"
    )
    assert len(granted_rows) == 1
    assert len(revoked_rows) == 1


async def test_patch_me_forbids_primary_consent_revocation(account_ctx):
    resp = account_ctx["client"].patch(
        "/api/user/me", headers=account_ctx["headers"], json={"consent": False}
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "CONSENT_REVOKE_FORBIDDEN"
    assert "/api/user/revoke-consent" in detail["hint"]

    # Отметка на месте, аккаунт жив, аудита нет.
    user = await _db_user(account_ctx["SessionLocal"], account_ctx["user_id"])
    assert user.consent_given_at is not None
    assert await _audit_rows(account_ctx["SessionLocal"], "consent_revoked") == []


async def test_patch_me_empty_body_is_422(account_ctx):
    resp = account_ctx["client"].patch(
        "/api/user/me", headers=account_ctx["headers"], json={}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "NOTHING_TO_UPDATE"


async def test_patch_me_requires_auth(account_ctx):
    resp = account_ctx["client"].patch("/api/user/me", json={"email": "x@example.com"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/user/personal-data — 152-ФЗ ст. 14
# ---------------------------------------------------------------------------


async def test_personal_data_includes_user_and_uploaded_files(account_ctx):
    await _seed_resume(
        account_ctx["SessionLocal"], account_ctx["user_id"], "resume.pdf"
    )

    resp = account_ctx["client"].get(
        "/api/user/personal-data", headers=account_ctx["headers"]
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["user"]["id"] == str(account_ctx["user_id"])
    assert body["user"]["email"] == "alice@example.com"
    assert len(body["resumes"]) == 1
    assert body["resumes"][0]["filename"] == "resume.pdf"
    assert body["resumes"][0]["size_bytes"] == 1024


async def test_personal_data_does_not_leak_other_users_files(account_ctx, make_user):
    stranger_id = await make_user("mallory@example.com", password=None)
    await _seed_resume(account_ctx["SessionLocal"], stranger_id, "stranger.pdf")
    await _seed_resume(account_ctx["SessionLocal"], account_ctx["user_id"], "mine.pdf")

    resp = account_ctx["client"].get(
        "/api/user/personal-data", headers=account_ctx["headers"]
    )
    assert resp.status_code == 200, resp.text
    assert [r["filename"] for r in resp.json()["resumes"]] == ["mine.pdf"]


async def test_personal_data_requires_auth(account_ctx):
    assert account_ctx["client"].get("/api/user/personal-data").status_code == 401


async def test_personal_data_audit_log_uses_x_forwarded_for(account_ctx):
    headers = {**account_ctx["headers"], "X-Forwarded-For": "1.2.3.4, 10.0.0.1"}

    resp = account_ctx["client"].get("/api/user/personal-data", headers=headers)
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows(account_ctx["SessionLocal"], "personal_data_export")
    assert len(rows) == 1
    # Первый хоп, обрезанный — не IP прокси и не пир ASGI.
    assert rows[0].ip_address == "1.2.3.4"
    assert rows[0].resource_id == str(account_ctx["user_id"])


# ---------------------------------------------------------------------------
# POST /api/user/revoke-consent — 152-ФЗ ст. 9 п. 2
# ---------------------------------------------------------------------------


async def test_revoke_consent_deletes_account_and_files(account_ctx):
    await _seed_resume(account_ctx["SessionLocal"], account_ctx["user_id"])

    resp = account_ctx["client"].post(
        "/api/user/revoke-consent",
        headers=account_ctx["headers"],
        json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    assert not await _user_exists(account_ctx["SessionLocal"], account_ctx["user_id"])
    async with account_ctx["SessionLocal"]() as session:
        left = await session.execute(
            select(Resume).where(Resume.user_id == account_ctx["user_id"])
        )
        assert list(left.scalars().all()) == [], "резюме должны уйти каскадом"


async def test_revoke_consent_without_confirm_changes_nothing(account_ctx):
    resp = account_ctx["client"].post(
        "/api/user/revoke-consent",
        headers=account_ctx["headers"],
        json={"confirm": False},
    )
    assert resp.status_code == 422

    assert await _user_exists(account_ctx["SessionLocal"], account_ctx["user_id"])
    assert await _audit_rows(account_ctx["SessionLocal"], "consent_revoked") == []


async def test_revoke_consent_requires_auth(account_ctx):
    resp = account_ctx["client"].post(
        "/api/user/revoke-consent", json={"confirm": True}
    )
    assert resp.status_code == 401


async def test_revoke_consent_audit_log_uses_x_forwarded_for(account_ctx):
    headers = {**account_ctx["headers"], "X-Forwarded-For": "5.6.7.8"}

    resp = account_ctx["client"].post(
        "/api/user/revoke-consent", headers=headers, json={"confirm": True}
    )
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows(
        account_ctx["SessionLocal"],
        "consent_revoked",
        "account_deleted_after_consent_revocation",
    )
    assert {r.action for r in rows} == {
        "consent_revoked",
        "account_deleted_after_consent_revocation",
    }
    assert {r.ip_address for r in rows} == {"5.6.7.8"}
    # Журнал переживает субъекта: строка о самом удалении обезличена.
    assert {r.user_id for r in rows} == {str(account_ctx["user_id"]), "[deleted]"}


# ---------------------------------------------------------------------------
# DELETE /api/user/account — 152-ФЗ ст. 21
# ---------------------------------------------------------------------------


async def test_delete_account_removes_user_and_cascades_to_resumes(account_ctx):
    await _seed_resume(account_ctx["SessionLocal"], account_ctx["user_id"])

    resp = account_ctx["client"].request(
        "DELETE",
        "/api/user/account",
        headers=account_ctx["headers"],
        json={"confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    async with account_ctx["SessionLocal"]() as session:
        remaining, total = await repo.list_user_resumes(
            session, user_id=account_ctx["user_id"]
        )
    assert remaining == []
    assert total == 0
    assert not await _user_exists(account_ctx["SessionLocal"], account_ctx["user_id"])


async def test_delete_account_invalidates_the_token(account_ctx):
    account_ctx["client"].request(
        "DELETE",
        "/api/user/account",
        headers=account_ctx["headers"],
        json={"confirm": True},
    )

    # Подпись всё ещё валидна, но субъекта нет — get_current_user даёт 401.
    after = account_ctx["client"].get("/api/user/me", headers=account_ctx["headers"])
    assert after.status_code == 401


async def test_delete_account_without_confirm_changes_nothing(account_ctx):
    resp = account_ctx["client"].request(
        "DELETE",
        "/api/user/account",
        headers=account_ctx["headers"],
        json={"confirm": False},
    )
    assert resp.status_code == 422

    assert await _user_exists(account_ctx["SessionLocal"], account_ctx["user_id"])
    assert await _audit_rows(account_ctx["SessionLocal"], "account_deleted") == []


async def test_delete_account_requires_auth(account_ctx):
    resp = account_ctx["client"].request(
        "DELETE", "/api/user/account", json={"confirm": True}
    )
    assert resp.status_code == 401


async def test_delete_account_audit_trail_records_request_and_completion(account_ctx):
    headers = {**account_ctx["headers"], "X-Forwarded-For": "9.9.9.9"}

    resp = account_ctx["client"].request(
        "DELETE", "/api/user/account", headers=headers, json={"confirm": True}
    )
    assert resp.status_code == 200, resp.text

    rows = await _audit_rows(
        account_ctx["SessionLocal"], "account_deletion_requested", "account_deleted"
    )
    assert {r.action for r in rows} == {
        "account_deletion_requested",
        "account_deleted",
    }
    assert {r.ip_address for r in rows} == {"9.9.9.9"}
    assert {r.resource_id for r in rows} == {str(account_ctx["user_id"])}
