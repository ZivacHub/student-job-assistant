"""The street map on the Mjesta page: the download button and the area that can be reached."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core import map_download
from app.core.db import get_session, get_sessionmaker
from app.core.map_areas import MAP_MODES, reach_rectangles
from app.core.places import MODE_LABELS, list_places
from app.core.profile import get_or_create_profile
from app.geo import mapstore
from app.notify.alerts import local_time

router = APIRouter()


def _address(url) -> str:
    return url.path + ("?" + url.query if url.query else "")


def streets_context(request: Request) -> dict:
    """What the Mjesta page says about the street map on this PC and about the download."""
    state = map_download.snapshot()
    files = mapstore.status()
    available = files[0].present  # the detailed area: without it there is no street map
    newest = max((f.downloaded_at for f in files if f.downloaded_at), default=None)
    stopped = request.query_params.get("auto") == "0"
    return {
        "state": "running" if state.running else ("ready" if available else "missing"),
        "available": available,
        "partial": available and not all(f.present for f in files),
        "needs_buses": available and files[0].bus_routes is None,  # map from before step 4.12
        "error": None if state.running else state.error,
        "step": state.step,
        "size_mb": round(sum(f.size_bytes for f in files) / 1e6),
        "when": local_time(newest) if newest else None,
        "auto_refresh": state.running and not stopped,
        "auto_stopped": state.running and stopped,
        "stop_auto_url": _address(request.url.include_query_params(auto="0")),
        "resume_url": _address(request.url.remove_query_params("auto")),
    }


def map_modes(places, has_car: bool) -> list[tuple[str, str]]:
    """The ways of travelling the map can show an area for, among those the places use."""
    used = {mode for p in places if p.active for mode in (p.max_minutes_by_mode or {})}
    return [(m, MODE_LABELS[m]) for m in MAP_MODES if m in used and (m != "car" or has_car)]


@router.post("/mjesta/karta/preuzmi")
async def download_map():
    started = map_download.start_download(get_sessionmaker())
    ok = "karta-pokrenuta" if started else "karta-vec"
    return RedirectResponse(f"/mjesta?ok={ok}#karta", status_code=303)


@router.get("/mjesta/podrucje")
def reach_areas(nacin: str = "walk", session: Session = Depends(get_session)):
    """The area every active place reaches in its allowed minutes, for one way of travelling.

    `street_map` says whether a map was available at all; the page falls back to its rough
    circles when it was not.
    """
    profile = get_or_create_profile(session)
    streets = mapstore.get_street_map()
    out: dict = {"mode": nacin, "street_map": streets is not None, "places": []}
    if streets is None or nacin not in MAP_MODES or (nacin == "car" and not profile.has_car):
        return JSONResponse(out)
    for place in list_places(session):
        minutes = (place.max_minutes_by_mode or {}).get(nacin)
        if not place.active or place.lat is None or place.lon is None or not minutes:
            continue
        rects = reach_rectangles(streets, nacin, (place.lat, place.lon), minutes)
        if rects:
            out["places"].append({"id": place.id, "label": place.label, "minutes": minutes,
                                  "rects": rects})
    return JSONResponse(out)
