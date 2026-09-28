"""Alembic environment: resolves DATABASE_URL (never hard-coded), imports
the runtime schema metadata, and degrades with a clear message when the
database is unreachable."""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

config = config = None  # set below; flake-friendly
from alembic import context  # noqa: E402

config = context.config

# Interpret the config file for Python logging when present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# DATABASE_URL is the only source of connection truth (Phase 8).
db_url = os.environ.get("DATABASE_URL", "").strip()
if not db_url:
    print("DATABASE_URL not set — alembic cannot run against a real database.\n"
          "Export DATABASE_URL (postgresql+psycopg://...) or a sqlite URL for local work.")
    sys.exit(1)
if db_url.startswith("postgres://"):  # legacy scheme normalization
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", db_url)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage"))

from storage.database import Base  # noqa: E402
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=db_url, target_metadata=target_metadata, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
