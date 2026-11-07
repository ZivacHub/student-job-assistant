from logging.config import fileConfig

from alembic import context
from sqlalchemy import text

from app.core.db import make_engine
from app.core.settings import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    # tests/scripts can pass -x url=... or set config main option; default = app settings
    return config.get_main_option("sqlalchemy.url") or get_settings().db_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    engine = make_engine(url)
    is_sqlite = url.startswith("sqlite")
    with engine.connect() as connection:
        if is_sqlite:
            # SQLite batch mode rebuilds tables (copy, drop, rename). With foreign keys enforced,
            # DROP TABLE cascades and silently deletes child rows (it deleted Ivan's CV rows once).
            # Turn enforcement off for the migration, then verify nothing is orphaned.
            connection.execute(text("PRAGMA foreign_keys=OFF"))
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite needs batch mode for ALTER
        )
        with context.begin_transaction():
            context.run_migrations()
        if is_sqlite:
            broken = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
            if broken:
                raise RuntimeError(f"migration left orphaned rows: {broken[:5]}")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
