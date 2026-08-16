import asyncio
import sys
import os
from logging.config import fileConfig
from pathlib import Path


from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Source tree moved from ``src/`` → ``backend/src/`` in Apr-2026. The
# wheel install already makes ``cv_customs`` importable after ``uv sync``,
# but we keep an explicit sys.path shim so ``alembic upgrade`` works even
# when the user runs it from a freshly-cloned checkout before installing.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _candidate in ("backend/src", "src"):
    _path = _REPO_ROOT / _candidate
    if _path.is_dir():
        sys.path.insert(0, str(_path))
        break

from cv_customs.db.models import Base

config = context.config
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)
else:
    print("!!! WARNING: DATABASE_URL is NOT SET. Alembic will use the placeholder from alembic.ini")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
