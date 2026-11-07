import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

from app.apply.replyflow import make_reply_watcher
from app.apply.runner import make_applier
from app.core.db import get_sessionmaker
from app.core.logging import setup_logging
from app.core.migrate import run_migrations
from app.core.mode import sync_from_profile
from app.core.settings import get_settings
from app.geo.mapstore import get_street_map
from app.geo.recheck import make_locator
from app.notify.dispatch import make_notifier
from app.notify.secrets import get_secret_store
from app.scheduler import Scheduler, default_sources
from app.web.routes import router as web_router
from app.web.security import LocalOnlyGuard

log = logging.getLogger(__name__)


class SchedulerLike(Protocol):
    def start(self) -> None: ...
    def shutdown(self, wait: bool = False) -> None: ...


def create_app(
    debug: bool | None = None, scheduler: SchedulerLike | None = None
) -> FastAPI:
    """Interactive API docs (/docs, /redoc, /openapi.json) exist only in debug mode, so a
    future LAN mode never hands the route list to everyone on the Wi-Fi.

    `scheduler` lets tests inject a stand-in; otherwise the real one is built at startup when
    `JOBS_SCHEDULER_ENABLED` is true.
    """
    settings = get_settings()
    debug = settings.debug if debug is None else debug

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        log.info("starting on %s:%s (dry_run=%s)", settings.host, settings.port, settings.dry_run)
        if settings.auto_migrate:
            run_migrations()
            setup_logging()  # idempotent; keeps our handlers after Alembic ran
        with get_sessionmaker()() as session:  # restore Ivan's choice: trial mode or real sending
            sync_from_profile(session)
            session.commit()
        # building the street graphs takes a few seconds: do it now, not at the first page
        threading.Thread(target=get_street_map, daemon=True, name="street-map-warmup").start()
        active = scheduler
        if active is None and settings.scheduler_enabled:
            active = Scheduler(
                get_sessionmaker(), default_sources(),
                startup_delay_s=settings.scheduler_startup_delay_s,
                notifier=make_notifier(
                    get_sessionmaker(), store_getter=get_secret_store,
                    base_url=f"http://{settings.host}:{settings.port}"),
                applier=make_applier(
                    get_sessionmaker(), store_getter=get_secret_store,
                    cv_dir_getter=lambda: settings.data_dir / "cvs"),
                locator=make_locator(get_sessionmaker()),
                reply_watcher=make_reply_watcher(get_sessionmaker(), store_getter=get_secret_store),
            )
        app.state.scheduler = active
        if active is not None:
            active.start()
        try:
            yield
        finally:
            if active is not None:
                active.shutdown()
            log.info("stopping")

    app = FastAPI(
        title="Student Job Assistant",
        lifespan=lifespan,
        docs_url="/docs" if debug else None,
        redoc_url="/redoc" if debug else None,
        openapi_url="/openapi.json" if debug else None,
    )
    app.state.scheduler = None
    # The approval gate's sender: None = build it from the saved Gmail password when needed
    # (app/apply/runner.py); tests put a pretend one here. Real sending is OFF until Ivan switches
    # it on on the Prijave page (app/core/mode.py).
    app.state.sender = None
    app.add_middleware(LocalOnlyGuard, extra_hosts=settings.allowed_hosts)
    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(web_router)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:  # some browsers ask for it before reading the <link>
        return Response(status_code=204)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "dry_run": settings.dry_run}

    return app


app = create_app()
