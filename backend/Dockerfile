# Backend-образ упрощённого сервиса: только web (uvicorn).
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir 'uv==0.11.*'

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Слой зависимостей: пересобирается только при изменении pyproject/uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Слой приложения: код и миграции.
COPY alembic.ini ./
COPY alembic ./alembic
COPY backend/src/cv_customs ./backend/src/cv_customs
RUN uv sync --frozen

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/backend/src

EXPOSE 8000

# shell-форма, чтобы локальный compose мог переопределить порт через $PORT;
# exec — чтобы uvicorn стал PID 1 и получал SIGTERM от docker stop.
CMD exec uvicorn cv_customs.main:app --host 0.0.0.0 --port ${PORT:-8000}
