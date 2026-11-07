from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """App settings. Override with environment variables prefixed JOBS_ or a local .env."""

    model_config = SettingsConfigDict(env_prefix="JOBS_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"  # local only by default; LAN access is opt-in (Phase 7)
    port: int = 8765
    data_dir: Path = ROOT / "data"
    database_url: str = ""
    geo_user_agent: str = "StudentJobAssistant/0.1 (personal local use)"
    debug: bool = False  # enables /docs; keep off
    allowed_hosts: list[str] = []  # extra host names besides localhost/127.0.0.1 (tests)
    scheduler_enabled: bool = True  # background polling of the job sources
    auto_migrate: bool = True  # bring the database up to date when the app starts
    scheduler_startup_delay_s: int = 5  # first poll this long after start
    dry_run: bool = True  # auto-send stays in dry-run until Ivan turns it off

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'jobs.db').as_posix()}"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    return s
