"""Page: Dodaj oglas (paste-in) - plan 2.7 + 4.4.

Step 1: paste the text (and a link if wanted). Step 2: see what the app understood, fix it, save.
The link is only stored, never opened. Nothing is saved before the second step.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.paste_form import (
    MAX_CITY,
    MAX_EMPLOYER,
    MAX_TITLE,
    PasteValues,
    build_preview,
    parse_first_step,
    parse_second_step,
)
from app.core.places import list_places
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.geo.cities import CITIES, DISPLAY
from app.geo.nominatim import Geocoder
from app.sources.pastein import MAX_CHARS, ingest_pasted
from app.web.templating import TEMPLATES

router = APIRouter()

KIND_LABEL = {"email": "E-pošta", "phone": "Telefon", "link": "Poveznica"}


def _towns() -> list[str]:
    return sorted({DISPLAY.get(key, key.title()) for key in CITIES})


def _page(request: Request, *, step: int, values: PasteValues, errors=None, preview=None):
    ctx = {
        "step": step,
        "values": values,
        "errors": errors or {},
        "preview": preview,
        "kind_label": KIND_LABEL,
        "towns": _towns(),
        "max_chars": MAX_CHARS,
        "max_title": MAX_TITLE,
        "max_employer": MAX_EMPLOYER,
        "max_city": MAX_CITY,
        "dry_run": get_settings().dry_run,
    }
    return TEMPLATES.TemplateResponse(request, "dodaj.html", ctx)


@router.get("/dodaj")
def paste_page(request: Request):
    return _page(request, step=1, values=PasteValues())


@router.post("/dodaj/pregled")
async def preview_step(request: Request):
    values, errors = parse_first_step(await request.form())
    if errors:
        return _page(request, step=1, values=values, errors=errors)
    preview = build_preview(values)
    return _page(request, step=2, values=preview.values, preview=preview)


@router.post("/dodaj/uredi")
async def back_to_text(request: Request):
    values, _ = parse_first_step(await request.form())
    return _page(request, step=1, values=values)


@router.post("/dodaj/spremi")
async def save_ad(request: Request, session: Session = Depends(get_session)):
    values, overrides, errors = parse_second_step(await request.form())
    if errors.get("text") or errors.get("url"):  # the text itself is missing: start over
        return _page(request, step=1, values=values, errors=errors)
    if errors:
        preview = build_preview(values)
        preview.values = values  # keep what was typed, not the guess
        return _page(request, step=2, values=values, errors=errors, preview=preview)
    profile = get_or_create_profile(session)
    places = [p for p in list_places(session) if p.active]
    # a pasted ad has no street address: the town table, plus a map search only when the title
    # names a place such as "TC Portanova" (step 4.10)
    with Geocoder(session) as geocoder:
        job, created = ingest_pasted(
            session, profile, places, geocoder, values.text, values.url or None, **overrides
        )
    if not created or job.duplicate_of:
        target = job.duplicate_of or job.id
        return RedirectResponse(f"/poslovi/{target}?ok=vec_postoji", status_code=303)
    return RedirectResponse(f"/poslovi/{job.id}?ok=dodano", status_code=303)
