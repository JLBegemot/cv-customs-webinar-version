"""Контракт TOTP-MFA (заглушка, живёт за флагом ``FEATURE_MFA``).

Роутер монтируется в боевое приложение только при включённом флаге,
поэтому тесты подают ``auth_mfa.router`` в одноразовое приложение
(``build_client(routers=[...])``) — так ветки хендлеров проверяются
независимо от того, как флаг выставлен в окружении. Отдельный тест
следит за самим флагом: при выключенном он не должен подмешивать
ручки в боевую таблицу роутов.

Проверяются три ветки: enrol выдаёт секрет и 10 одноразовых кодов,
verify принимает только текущий код, regenerate полностью заменяет
набор кодов. Плюс отказы: без enrol'а verify/regenerate — 400, без
токена — 401.
"""

from __future__ import annotations

import pytest

from cv_customs.db import repositories as repo


@pytest.fixture
async def mfa_ctx(build_client, session_factory, make_user, auth_headers):
    pytest.importorskip("pyotp")

    from cv_customs.api import auth_mfa

    user_id = await make_user("mfa@example.com")

    return {
        "client": build_client(routers=[auth_mfa.router]),
        "headers": auth_headers(user_id),
        "SessionLocal": session_factory,
        "user_id": user_id,
    }


def _secret_from(provisioning_uri: str) -> str:
    return provisioning_uri.split("secret=")[1].split("&")[0]


# ---------------------------------------------------------------------------
# Enroll
# ---------------------------------------------------------------------------


async def test_enroll_returns_provisioning_uri_and_recovery_codes(mfa_ctx):
    resp = mfa_ctx["client"].post("/api/v1/auth/mfa/enroll", headers=mfa_ctx["headers"])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["provisioning_uri"].startswith("otpauth://totp/")
    assert "issuer=CV%20Customs" in body["provisioning_uri"]
    assert len(body["recovery_codes"]) == 10
    # Коды одноразовые — повтор внутри набора обесценил бы половину из них.
    assert len(set(body["recovery_codes"])) == 10


async def test_enroll_persists_the_secret_on_the_user(mfa_ctx):
    resp = mfa_ctx["client"].post("/api/v1/auth/mfa/enroll", headers=mfa_ctx["headers"])
    secret = _secret_from(resp.json()["provisioning_uri"])

    async with mfa_ctx["SessionLocal"]() as session:
        user = await repo.get_user_by_id(session, mfa_ctx["user_id"])
    assert user is not None
    assert user.mfa_secret == secret


async def test_enroll_never_returns_the_raw_secret_field(mfa_ctx):
    body = (
        mfa_ctx["client"]
        .post("/api/v1/auth/mfa/enroll", headers=mfa_ctx["headers"])
        .json()
    )
    # Секрет уезжает клиенту только внутри otpauth-URI/QR, отдельным полем —
    # нет: лишняя копия в логах и в state SPA никому не нужна.
    assert "secret" not in body
    assert set(body) == {"provisioning_uri", "qr_png_data_url", "recovery_codes"}


async def test_enroll_requires_auth(mfa_ctx):
    assert mfa_ctx["client"].post("/api/v1/auth/mfa/enroll").status_code == 401


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


async def test_verify_accepts_current_code(mfa_ctx):
    import pyotp

    enroll = mfa_ctx["client"].post(
        "/api/v1/auth/mfa/enroll", headers=mfa_ctx["headers"]
    )
    code = pyotp.TOTP(_secret_from(enroll.json()["provisioning_uri"])).now()

    resp = mfa_ctx["client"].post(
        "/api/v1/auth/mfa/verify", json={"code": code}, headers=mfa_ctx["headers"]
    )
    assert resp.status_code == 204, resp.text


async def test_verify_rejects_wrong_code(mfa_ctx):
    mfa_ctx["client"].post("/api/v1/auth/mfa/enroll", headers=mfa_ctx["headers"])

    resp = mfa_ctx["client"].post(
        "/api/v1/auth/mfa/verify",
        json={"code": "000000"},
        headers=mfa_ctx["headers"],
    )
    assert resp.status_code == 401


async def test_verify_before_enrolment_is_a_400_not_a_401(mfa_ctx):
    """«MFA не подключена» — это состояние аккаунта, а не отказ в доступе:
    клиенту нужен именно 400, чтобы отправить пользователя на enrol."""

    resp = mfa_ctx["client"].post(
        "/api/v1/auth/mfa/verify",
        json={"code": "123456"},
        headers=mfa_ctx["headers"],
    )
    assert resp.status_code == 400, resp.text
    assert "not enrolled" in resp.json()["detail"]


async def test_verify_requires_auth(mfa_ctx):
    resp = mfa_ctx["client"].post("/api/v1/auth/mfa/verify", json={"code": "123456"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


async def test_recovery_codes_regenerate_replaces_set(mfa_ctx):
    first = (
        mfa_ctx["client"]
        .post("/api/v1/auth/mfa/enroll", headers=mfa_ctx["headers"])
        .json()
    )
    second = (
        mfa_ctx["client"]
        .post("/api/v1/auth/mfa/recovery-codes/regenerate", headers=mfa_ctx["headers"])
        .json()
    )

    assert set(first["recovery_codes"]).isdisjoint(set(second["recovery_codes"]))
    assert len(second["recovery_codes"]) == 10


async def test_recovery_codes_regenerate_before_enrolment_is_400(mfa_ctx):
    resp = mfa_ctx["client"].post(
        "/api/v1/auth/mfa/recovery-codes/regenerate", headers=mfa_ctx["headers"]
    )
    assert resp.status_code == 400, resp.text


async def test_recovery_codes_regenerate_requires_auth(mfa_ctx):
    resp = mfa_ctx["client"].post("/api/v1/auth/mfa/recovery-codes/regenerate")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_on", [False, True])
def test_mount_if_enabled_follows_the_feature_flag(monkeypatch, flag_on):
    """При выключенном флаге ручек не существует вовсе — 404, а не 503.

    Проверяется сама ``mount_if_enabled`` на свежем ``FastAPI()``: в боевом
    приложении флаг читается один раз на импорте ``main``, поэтому подмена
    настройки после импорта там ничего бы не изменила и тест зеленел бы по
    чужой причине.
    """

    from fastapi import FastAPI

    from cv_customs.api import auth_mfa
    from cv_customs.config import settings

    monkeypatch.setattr(settings, "feature_mfa", flag_on)
    app = FastAPI()
    auth_mfa.mount_if_enabled(app)

    mfa_paths = [path for path in app.openapi()["paths"] if "/mfa/" in path]
    assert bool(mfa_paths) is flag_on
