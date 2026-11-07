"""Pages: Poslovi (the inbox) and Mjesta i profil (places and profile)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.apply_view import waiting_count
from app.core.db import get_session
from app.core.inbox import (
    CHANNEL_LABELS,
    SCORE_STEPS,
    VIEW_LABELS,
    load_inbox,
    parse_filters,
)
from app.core.job_actions import wake_due_snoozed
from app.core.places import (
    KIND_LABELS,
    MAX_BONUS,
    MAX_MINUTES,
    MODE_LABELS,
    PlaceForm,
    delete_place,
    list_places,
    map_data,
    parse_place_form,
    place_summary,
    save_place,
    town_options,
)
from app.core.profile import get_or_create_profile
from app.core.profile_form import CV_COUNTRIES, TONES, parse_profile_form, save_profile
from app.core.refresh_view import back_address, refresh_notice, refresh_outcome
from app.core.settings import get_settings
from app.core.sources_view import (
    broken_sources,
    parse_since,
    refresh_all,
    refresh_state,
)
from app.geo import mapstore
from app.models.base import utcnow
from app.scoring.parse import CATEGORY_HR
from app.scoring.rescore import rescore_jobs
from app.web.apply_routes import router as apply_router
from app.web.gmail_routes import router as gmail_router
from app.web.job_routes import router as job_router
from app.web.log_routes import router as log_router
from app.web.map_routes import map_modes, streets_context
from app.web.map_routes import router as map_router
from app.web.paste_routes import router as paste_router
from app.web.source_routes import router as source_router
from app.web.templating import TEMPLATES

router = APIRouter()
router.include_router(job_router)
router.include_router(apply_router)
router.include_router(gmail_router)
router.include_router(source_router)
router.include_router(paste_router)
router.include_router(log_router)
router.include_router(map_router)

FLASH = {
    "mjesto": "Mjesto je spremljeno.",
    "obrisano": "Mjesto je obrisano.",
    "profil": "Profil je spremljen.",
    "karta-pokrenuta": "Preuzimanje karte ulica je počelo.",
    "karta-vec": "Preuzimanje karte ulica već traje.",
}


def _scheduler(request: Request):
    return getattr(request.app.state, "scheduler", None)


def _with_param(request: Request, key: str, value: str) -> str:
    url = request.url.include_query_params(**{key: value})
    return url.path + ("?" + url.query if url.query else "")


def _without_param(request: Request, key: str) -> str:
    url = request.url.remove_query_params(key)
    return url.path + ("?" + url.query if url.query else "")


def _page(request: Request, session: Session, **extra):
    profile = get_or_create_profile(session)
    places = list_places(session)
    ctx = {
        "streets": streets_context(request),
        "map_modes": map_modes(places, profile.has_car),
        "profile": profile,
        "places": places,
        "summaries": {p.id: place_summary(p, profile.has_car) for p in places},
        "map_data": map_data(places, profile.has_car),
        "towns": town_options(),
        "kinds": KIND_LABELS,
        "modes": MODE_LABELS,
        "categories": CATEGORY_HR,
        "tones": TONES,
        "cv_countries": CV_COUNTRIES,
        "max_bonus": MAX_BONUS,
        "max_minutes": MAX_MINUTES,
        "dry_run": get_settings().dry_run,
        "flash": None,
        "errors": {},
        "open_form": None,
        "place_values": None,
        "profile_values": None,
        **extra,
    }
    return TEMPLATES.TemplateResponse(request, "places.html", ctx)


@router.get("/")
def home():
    return RedirectResponse("/poslovi", status_code=303)


@router.get("/poslovi")
def inbox_page(request: Request, session: Session = Depends(get_session)):
    wake_due_snoozed(session)  # snoozed ads whose time has come are open again
    session.commit()
    profile = get_or_create_profile(session)
    filters = parse_filters(request.query_params)
    has_places = any(p.active for p in list_places(session))
    inbox = load_inbox(session, filters, profile, has_places=has_places)
    ok = request.query_params.get("ok")
    since = parse_since(request.query_params.get("od")) if ok == "osvjezeno" else None
    state = refresh_state(session, _scheduler(request), since)
    stopped = request.query_params.get("auto") == "0"  # the user stopped the automatic reload
    auto = state.running and not stopped
    return TEMPLATES.TemplateResponse(request, "poslovi.html", {
        "inbox": inbox,
        "filters": inbox.filters,
        "refresh": state,
        "refresh_notice": refresh_notice(ok, state, auto=not stopped),
        "auto_refresh": auto,
        "auto_stopped": state.running and stopped,
        "stop_auto_url": _with_param(request, "auto", "0"),
        "resume_url": _without_param(request, "auto"),
        "views": VIEW_LABELS,
        "channels": CHANNEL_LABELS,
        "score_steps": SCORE_STEPS,
        "broken_sources": broken_sources(session, _scheduler(request)),
        "streets_missing": has_places and not mapstore.status()[0].present,
        "drafts_waiting": waiting_count(session),
        "dry_run": get_settings().dry_run,
    })


@router.post("/poslovi/osvjezi")
async def refresh_inbox(request: Request, session: Session = Depends(get_session)):
    """The "Osvježi" button: check every enabled source for new ads now, then show the list."""
    form = await request.form()
    keep = parse_filters({k: str(form.get(k) or "") for k in ("prikaz", "kanal", "ocjena")})
    result = refresh_all(session, _scheduler(request))
    session.commit()
    address = back_address(keep.query(page=1), ok=refresh_outcome(result),
                           od=int(utcnow().timestamp()))
    return RedirectResponse(address, status_code=303)


@router.get("/mjesta")
def places_page(request: Request, ok: str | None = None, session: Session = Depends(get_session)):
    flash = FLASH.get(ok or "")
    n = request.query_params.get("n")
    if flash and n and n.isdigit() and ok in ("mjesto", "obrisano", "profil"):
        count = int(n)
        flash += f" Ocjena je ponovno izračunata za {count} {_oglasa(count)}." if count else ""
    return _page(request, session, flash=flash)


def _oglasa(n: int) -> str:
    return "oglas" if n % 10 == 1 and n % 100 != 11 else "oglasa"


async def _rescore(session: Session) -> int:
    profile = get_or_create_profile(session)
    return rescore_jobs(session, profile, [p for p in list_places(session) if p.active])


@router.post("/mjesta/mjesto")
async def save_place_route(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    raw_id = (form.get("id") or "").strip()
    place_id = int(raw_id) if raw_id.isdigit() else None
    values, errors = parse_place_form(form)
    if errors:
        return _page(request, session, errors=errors, place_values=values,
                     open_form=place_id or "new")
    try:
        save_place(session, values, place_id)
    except LookupError:
        return _page(request, session,
                     errors={"label": "Mjesto ne postoji (možda je već obrisano)."},
                     place_values=values, open_form="new")
    n = await _rescore(session)
    session.commit()
    return RedirectResponse(f"/mjesta?ok=mjesto&n={n}", status_code=303)


@router.post("/mjesta/mjesto/{place_id}/obrisi")
async def delete_place_route(place_id: int, session: Session = Depends(get_session)):
    delete_place(session, place_id)
    n = await _rescore(session)
    session.commit()
    return RedirectResponse(f"/mjesta?ok=obrisano&n={n}", status_code=303)


@router.post("/mjesta/profil")
async def save_profile_route(request: Request, session: Session = Depends(get_session)):
    profile = get_or_create_profile(session)
    form = await request.form()
    values, errors = parse_profile_form(form, [cv.id for cv in profile.cvs])
    if errors:
        return _page(request, session, errors=errors, profile_values=values)
    save_profile(session, profile, values)
    n = await _rescore(session)
    session.commit()
    return RedirectResponse(f"/mjesta?ok=profil&n={n}", status_code=303)


__all__ = ["router", "PlaceForm"]
