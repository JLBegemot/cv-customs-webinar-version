# cv-customs (webinar edition)

Сервис cv-customs: **авторизация, загрузка файлов резюме, настройки
пользователя**. Файлы хранятся как есть: метаданные в Postgres +
блоб в S3/MinIO.

> **Только локальный запуск.** Сервис не рассчитан на публичный доступ —
> выставлять его наружу нельзя. Это осознанное ограничение.

## Поверхность API

| Группа | Ручки |
|---|---|
| Auth | `POST /api/auth/{register,login}`, `POST /api/v1/auth/{register,login,logout}` |
| Password reset | `POST /api/v1/auth/password/reset/{request,confirm}` |
| MFA (за флагом `FEATURE_MFA`) | `POST /api/v1/auth/mfa/{enroll,verify,recovery-codes/regenerate}` |
| Профиль | `GET /api/user/me`, `GET /api/user/personal-data`, `POST /api/user/revoke-consent`, `DELETE /api/user/account` |
| Файлы резюме | `POST /api/v1/resumes/upload`, `GET /api/v1/resumes`, `GET /api/v1/resumes/{id}`, `GET /api/v1/resumes/{id}/original`, `DELETE /api/v1/resumes/{id}` |
| Legal | `GET /api/legal/*` |

Регистрация сразу возвращает JWT — email-верификации нет. Снапшот контракта —
[docs/openapi.json](docs/openapi.json), перегенерация:
`uv run python scripts/openapi_snapshot.py --dump`.

## Локальный запуск

```bash
# 1. Окружение
cp .env.example .env        # заполнить JWT_SECRET (≥32 символов)
uv sync --extra dev

# 2. Инфраструктура: Postgres + MinIO
docker compose up -d postgres minio minio-bootstrap

# 3. Миграции
uv run alembic upgrade head

# 4. Сервер
PYTHONPATH=backend/src uv run uvicorn cv_customs.main:app --reload
```

Либо всё в докере (API соберётся из `backend/Dockerfile` и сам прогонит
миграции):

```bash
docker compose up -d
```

## Тесты и smoke

```bash
uv run pytest -q                                    # юнит/API-тесты (офлайн)
uv run python scripts/openapi_snapshot.py --check   # контракт не отстал
./scripts/smoke-local.sh                            # E2E: register→login→upload→download→delete
```

## Структура

```
backend/src/cv_customs/   # FastAPI-приложение (описание — .claude/skills/python-backend-fastapi)
alembic/                  # одна baseline-миграция: users, audit_log, resumes
tests/                    # pytest; правила — docs/test-rules.md
docs/openapi.json         # снапшот контракта
scripts/                  # openapi_snapshot, validate_cases, smoke-local
.claude/                  # скиллы (/api-tests, /ship, python-backend-fastapi),
                          # сабагенты, хуки
```

Генерация API-тестов — скилл `/api-tests` (согласование кейсов → cases.yaml →
test-writer → test-reviewer), правила тестов — единственный источник —
[docs/test-rules.md](docs/test-rules.md).
