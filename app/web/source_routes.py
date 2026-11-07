"""Page: Izvori (sources of ads, how they are doing, what Ivan may change) - plan 4.4.

Nothing here talks to a website itself: "Provjeri sada" asks the scheduler to do its normal,
polite run in the background (robots.txt and waiting times included).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.settings import get_settings
from app.core.sources_view import (
    INTERVAL_CHOICES,
    SettingsError,
    any_running,
    load_sources,
    save_source_settings,
    start_run,
)
from app.models import Source
from app.web.templating import TEMPLATES

router = APIRouter()

FLASH = {
    "spremljeno": "Postavke izvora su spremljene.",
    "pokrenuto": "Provjera je pokrenuta. Može potrajati nekoliko minuta; stranica se sama "
                 "osvježava.",
}
RUN_ERRORS = {
    "busy": "Taj se izvor već provjerava. Pričekajte da završi.",
    "recent": "Taj je izvor provjeren prije manje od minute. Pričekajte malo, da se stranicu ne "
              "opterećuje bez potrebe.",
    "off": "Izvor je isključen. Najprije ga uključite.",
    "no_collector": "Ova verzija programa nema čitač za taj izvor, pa se ne može provjeriti.",
    "no_scheduler": "Automatsko provjeravanje nije uključeno u ovom pokretanju programa, pa se "
                    "provjera ne može pokrenuti.",
}


def _scheduler(request: Request):
    return getattr(request.app.state, "scheduler", None)


def _with_param(request: Request, key: str, value: str) -> str:
    url = request.url.include_query_params(**{key: value})
    return url.path + ("?" + url.query if url.query else "")


def _not_found(request: Request):
    return TEMPLATES.TemplateResponse(
        request, "nije_pronadeno.html",
        {"dry_run": get_settings().dry_run, "nav": "izvori", "missing": "Izvor nije pronađen",
         "back_url": "/izvori", "back_label": "Natrag na izvore"},
        status_code=404,
    )


def _page(request: Request, session: Session, **extra):
    sched = _scheduler(request)
    rows = load_sources(session, sched)
    settings = get_settings()
    ctx = {
        "rows": rows,
        "intervals": INTERVAL_CHOICES,
        "auto_refresh": any_running(rows) and request.query_params.get("auto") != "0",
        "stop_auto_url": _with_param(request, "auto", "0"),
        "running_now": any_running(rows),
        "scheduler_on": bool(sched is not None and getattr(sched, "running", False)),
        "dry_run": settings.dry_run,
        "host": settings.host,
        "port": settings.port,
        "flash": None,
        "error": None,
        "error_key": None,
        **extra,
    }
    return TEMPLATES.TemplateResponse(request, "izvori.html", ctx)


@router.get("/izvori")
def sources_page(request: Request, ok: str | None = None, session: Session = Depends(get_session)):
    return _page(request, session, flash=FLASH.get(ok or ""))


def _source(session: Session, key: str) -> Source | None:
    return session.scalar(select(Source).where(Source.key == key))


@router.post("/izvori/{key}/postavke")
async def save_settings(key: str, request: Request, session: Session = Depends(get_session)):
    if _source(session, key) is None:
        return _not_found(request)
    form = await request.form()
    try:
        interval = int(str(form.get("interval") or "").strip())
    except ValueError:
        interval = -1
    try:
        save_source_settings(session, key, enabled=form.get("enabled") == "on", interval=interval)
    except SettingsError as exc:
        return _page(request, session, error=str(exc), error_key=key)
    session.commit()
    sched = _scheduler(request)
    if sched is not None and hasattr(sched, "sync_source"):
        sched.sync_source(key)
    return RedirectResponse("/izvori?ok=spremljeno", status_code=303)


@router.post("/izvori/{key}/pokreni")
async def run_now(key: str, request: Request, session: Session = Depends(get_session)):
    if _source(session, key) is None:
        return _not_found(request)
    result = start_run(session, _scheduler(request), key)
    session.commit()
    if result == "started":
        return RedirectResponse("/izvori?ok=pokrenuto", status_code=303)
    if result == "unknown":
        return _not_found(request)
    return _page(request, session, error=RUN_ERRORS[result], error_key=key)
