import logging
from logging.handlers import RotatingFileHandler

from app.core.settings import get_settings


def setup_logging() -> None:
    settings = get_settings()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    if any(getattr(h, "_jobs_handler", False) for h in root.handlers):
        return  # already configured
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(
        settings.log_dir / "app.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    console = logging.StreamHandler()
    for h in (file_handler, console):
        h.setFormatter(fmt)
        h._jobs_handler = True  # type: ignore[attr-defined]
        root.addHandler(h)
