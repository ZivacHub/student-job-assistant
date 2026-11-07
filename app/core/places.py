"""Ivan's places (home, partner, faculty ...): validating the form and saving.

A place is set by clicking on the map or by picking a town from the offline list. Its position is
never sent to an online geocoder (plan 4.2, audit A-13).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.geo.cities import CITIES, DISPLAY
from app.geo.travel import MODE_SPEED, reach_km
from app.models import Place, PlaceKind

KIND_LABELS = {
    PlaceKind.HOME.value: "Dom",
    PlaceKind.PARTNER.value: "Partner",
    PlaceKind.UNI.value: "Fakultet",
    PlaceKind.OTHER.value: "Drugo",
}
MODE_LABELS = {"car": "Autom", "bus": "Autobusom", "bike": "Biciklom", "walk": "Pješice",
               "tram": "Tramvajem"}
MODE_LABELS_LOWER = {"car": "autom", "bus": "autobusom", "bike": "biciklom", "walk": "pješice",
                     "tram": "tramvajem"}
MAX_MINUTES = 240
MAX_BONUS = 15
MAX_LABEL = 60
# generous box around Croatia; catches swapped or mistyped coordinates
LAT_RANGE = (42.2, 46.7)
LON_RANGE = (13.3, 19.6)
CUSTOM_POINT = "Točka na karti"


@dataclass
class PlaceForm:
    label: str = ""
    kind: str = PlaceKind.OTHER.value
    lat: float | None = None
    lon: float | None = None
    town: str = ""  # key in the offline town list, if picked
    minutes: dict[str, int] = field(default_factory=dict)
    bonus: int = 0
    active: bool = True


def town_options() -> list[dict]:
    """[{key, name, lat, lon}] for the town drop-down, sorted by name."""
    items = [
        {"key": key, "name": DISPLAY.get(key, key.title()), "lat": lat, "lon": lon}
        for key, (lat, lon) in CITIES.items()
    ]
    return sorted(items, key=lambda t: t["name"].lower())


def _num(text: str | None) -> float | None:
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    return float(text)


def parse_place_form(form: Mapping[str, str]) -> tuple[PlaceForm, dict[str, str]]:
    """Read and check the place form. Returns the values and {field: error in Croatian}."""
    errors: dict[str, str] = {}
    out = PlaceForm(
        label=" ".join((form.get("label") or "").split()),
        kind=form.get("kind") or PlaceKind.OTHER.value,
        town=form.get("town") or "",
        active=form.get("active") in ("on", "1", "true"),
    )
    if not out.label:
        errors["label"] = "Upišite naziv mjesta, npr. Dom."
    elif len(out.label) > MAX_LABEL:
        errors["label"] = f"Naziv može imati najviše {MAX_LABEL} znakova."
    if out.kind not in KIND_LABELS:
        out.kind = PlaceKind.OTHER.value

    try:
        out.lat, out.lon = _num(form.get("lat")), _num(form.get("lon"))
    except ValueError:
        errors["lat"] = "Zemljopisna širina i dužina moraju biti brojevi, npr. 45.5550."
    if "lat" not in errors and (out.lat is None) != (out.lon is None):
        errors["lat"] = "Upišite i širinu i dužinu, ili kliknite na kartu."
    if out.lat is None and out.lon is None and "lat" not in errors:
        if out.town in CITIES:
            out.lat, out.lon = CITIES[out.town]
        else:
            errors["lat"] = "Odaberite grad ili kliknite na kartu da postavite mjesto."
    if out.lat is not None and out.lon is not None:
        in_croatia = (LAT_RANGE[0] <= out.lat <= LAT_RANGE[1]
                      and LON_RANGE[0] <= out.lon <= LON_RANGE[1])
        if not in_croatia:
            errors["lat"] = ("Ta točka nije u Hrvatskoj. Provjerite jesu li širina (oko 45) "
                             "i dužina (oko 18) upisane ispravnim redom.")

    for mode in MODE_SPEED:
        raw = (form.get(f"min_{mode}") or "").strip()
        if not raw:
            continue
        try:
            minutes = int(raw)
        except ValueError:
            errors[f"min_{mode}"] = "Upišite broj minuta."
            continue
        if not 1 <= minutes <= MAX_MINUTES:
            errors[f"min_{mode}"] = f"Minute moraju biti između 1 i {MAX_MINUTES}."
        else:
            out.minutes[mode] = minutes
    if not out.minutes and not any(k.startswith("min_") for k in errors):
        errors["min_car"] = "Upišite barem jedan način putovanja i koliko minuta smijete putovati."

    raw_bonus = (form.get("bonus") or "0").strip() or "0"
    try:
        out.bonus = int(raw_bonus)
        if not 0 <= out.bonus <= MAX_BONUS:
            errors["bonus"] = f"Bonus bodovi moraju biti između 0 i {MAX_BONUS}."
    except ValueError:
        errors["bonus"] = "Upišite cijeli broj bodova."
    return out, errors


def save_place(session: Session, data: PlaceForm, place_id: int | None = None) -> Place:
    place = session.get(Place, place_id) if place_id else None
    if place_id and place is None:
        raise LookupError("Mjesto ne postoji (možda je već obrisano).")
    if place is None:
        place = Place()
        session.add(place)
    place.label, place.kind = data.label, data.kind
    place.lat, place.lon = data.lat, data.lon
    place.address = DISPLAY.get(data.town, data.town.title()) if data.town in CITIES else None
    place.max_minutes_by_mode = dict(data.minutes)
    place.bonus_points, place.active = data.bonus, data.active
    session.flush()
    return place


def delete_place(session: Session, place_id: int) -> bool:
    place = session.get(Place, place_id)
    if place is None:
        return False
    session.delete(place)
    session.flush()
    return True


def list_places(session: Session) -> list[Place]:
    return list(session.scalars(select(Place).order_by(Place.id)))


def place_summary(place: Place, has_car: bool) -> str:
    """One line for the list: 'autobusom 40 min · pješice 20 min · +10 bodova'."""
    parts = []
    for mode in MODE_LABELS:
        minutes = (place.max_minutes_by_mode or {}).get(mode)
        if minutes and (mode != "car" or has_car):
            parts.append(f"{MODE_LABELS_LOWER[mode]} {minutes} min")
    if place.bonus_points:
        parts.append(f"+{place.bonus_points} bodova")
    if (place.max_minutes_by_mode or {}).get("car") and not has_car:
        parts.append("auto se ne računa (nemate auto)")
    return " · ".join(parts)


def map_data(places: list[Place], has_car: bool) -> list[dict]:
    """What the map needs: a marker and a circle (the farthest you can get) per place."""
    out = []
    for p in places:
        if p.lat is None or p.lon is None:
            continue
        reach = [
            reach_km(mode, minutes)
            for mode, minutes in (p.max_minutes_by_mode or {}).items()
            if mode in MODE_SPEED and (mode != "car" or has_car)
        ]
        out.append({
            "id": p.id, "label": p.label, "lat": p.lat, "lon": p.lon, "active": bool(p.active),
            "radius_m": round(max(reach, default=0.0) * 1000),
            # the ways this place is used with, for the map: which area to ask for
            "minutes": {
                mode: minutes for mode, minutes in (p.max_minutes_by_mode or {}).items()
                if mode in MODE_SPEED and (mode != "car" or has_car)
            },
        })
    return out
