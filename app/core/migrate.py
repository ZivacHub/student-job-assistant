"""Bring the database up to date (Alembic), from inside the app or from scripts."""

import logging

from alembic import command
from alembic.config import Config

from app.core.settings import ROOT, get_settings

log = logging.getLogger(__name__)


def run_migrations(url: str | None = None) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().db_url)
    cfg.attributes["configure_logger"] = False  # keep the app's own logging setup
    command.upgrade(cfg, "head")
    log.info("database is up to date")
