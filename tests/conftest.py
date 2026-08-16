"""Общий стенд для тестов cv-customs (webinar edition).

Здесь живёт вся обвязка, которую раньше каждый тест-файл собирал сам:
SQLite-in-memory, подмена lifespan-ресурсов приложения, JWT-заголовки.
Тест-файл добавляет только сидинг своих данных — стенд он не пересобирает.

Два варианта приложения:

* ``build_client()`` без аргументов — боевое ``cv_customs.main.app`` со всей
  таблицей роутов (нужно там, где тест ходит по нескольким роутерам сразу);
* ``build_client(routers=[...])`` — одноразовое ``FastAPI()`` только из
  указанных роутеров (быстрее и не зависит от feature-флагов, которые
  решают, монтировать ли роутер в боевое приложение).

Оба варианта после теста откатывают всё, что тронули: ``dependency_overrides``,
``lifespan_context`` и ключи ``app.state`` — иначе боевое приложение,
живущее модульным синглтоном, утаскивает состояние в соседние тесты.
"""

from __future__ import annotations

import os

# Должно отработать до импорта cv_customs.config, который читает окружение.
os.environ.setdefault("JWT_SECRET", "test-secret-key-must-be-at-least-32-chars-long")
os.environ.setdefault("JWT_EXPIRE_HOURS", "4")

from contextlib import asynccontextmanager  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from cv_customs.db import Base, User  # noqa: E402

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover — fastapi extra не установлен
    TestClient = None  # type: ignore[assignment]


_UNSET = object()

# Ключи app.state, которые lifespan заполняет в проде, а стенд подменяет.
_STATE_KEYS = ("email", "s3")


# ---------------------------------------------------------------------------
# БД
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    """Свежая SQLite-in-memory со всеми таблицами — на каждый тест своя."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(db_engine):
    """``async_sessionmaker`` поверх тестового движка."""

    return async_sessionmaker(db_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Внешние ресурсы: почта
# ---------------------------------------------------------------------------


@pytest.fixture
def email_client():
    """Почтовый клиент-заглушка: копит отправленное в ``.sent``."""

    from cv_customs.infra import NullEmailClient

    return NullEmailClient()


# ---------------------------------------------------------------------------
# Сидинг и авторизация
# ---------------------------------------------------------------------------


@pytest.fixture
async def make_user(session_factory):
    """Фабрика пользователей: ``await make_user(email=...)`` → ``user_id``.

    ``password=None`` оставляет ``password_hash`` пустым — так заводится
    пользователь, который в тесте не логинится.
    """

    async def _make(
        email: str = "user@example.com",
        *,
        password: str | None = "pw",
        consent: bool = True,
        **columns,
    ):
        now = datetime.now(timezone.utc)
        password_hash = None
        if password is not None:
            import bcrypt

            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        async with session_factory() as session:
            user = User(
                email=email,
                password_hash=password_hash,
                consent_given_at=now if consent else None,
                **columns,
            )
            session.add(user)
            await session.commit()
            return user.id

    return _make


@pytest.fixture
def auth_headers():
    """Фабрика заголовков: ``auth_headers(user_id)`` → ``{"Authorization": ...}``."""

    from cv_customs.api.auth import create_access_token

    def _make(user_id):
        return {"Authorization": f"Bearer {create_access_token(user_id)}"}

    return _make


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------


@pytest.fixture
async def build_client(session_factory):
    """Фабрика ``TestClient``, собранного поверх стенда.

    Параметры соответствуют ключам ``app.state``, которые в проде заполняет
    lifespan: ``email``, ``s3``. По умолчанию оба ``None`` — это ветки
    «внешнего сервиса нет», которые тесты и должны гонять.
    """

    if TestClient is None:
        pytest.skip("fastapi.testclient not available")
    pytest.importorskip("httpx")

    from cv_customs.db import get_session as real_get_session

    opened: list[tuple] = []

    def _build(*, routers=None, email=None, s3=None):
        if routers is None:
            from cv_customs.main import app
        else:
            from fastapi import FastAPI

            app = FastAPI()
            for router in routers:
                app.include_router(router)

        saved_state = {key: getattr(app.state, key, _UNSET) for key in _STATE_KEYS}
        saved_lifespan = app.router.lifespan_context

        app.state.email = email
        app.state.s3 = s3

        async def override_get_session():
            async with session_factory() as session:
                yield session

        app.dependency_overrides[real_get_session] = override_get_session

        @asynccontextmanager
        async def noop_lifespan(_app):
            yield

        app.router.lifespan_context = noop_lifespan  # type: ignore[assignment]

        client_cm = TestClient(app)
        client = client_cm.__enter__()
        opened.append((app, saved_state, saved_lifespan, client_cm))
        return client

    try:
        yield _build
    finally:
        for app, saved_state, saved_lifespan, client_cm in reversed(opened):
            client_cm.__exit__(None, None, None)
            app.dependency_overrides.pop(real_get_session, None)
            app.router.lifespan_context = saved_lifespan  # type: ignore[assignment]
            for key, value in saved_state.items():
                if value is _UNSET:
                    if hasattr(app.state, key):
                        delattr(app.state, key)
                else:
                    setattr(app.state, key, value)
