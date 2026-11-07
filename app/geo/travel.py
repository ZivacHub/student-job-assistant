"""Travel times from a place to a job.

Two sources: the street map on this PC (`streets.py`, step 4.9) when it has been downloaded and
covers both ends, and otherwise the old rough estimate below (straight line x 1.3 at an average
speed). A time that comes from the rough estimate is marked `approx` all the way to the screen.
"""

import logging
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from app.geo.streets import INF

log = logging.getLogger(__name__)

DETOUR_FACTOR = 1.3  # road distance ~ straight line x 1.3

# (average km/h, fixed minutes added for parking / waiting / walking to the stop)
MODE_SPEED: dict[str, tuple[float, float]] = {
    "car": (45.0, 5.0),
    "bus": (25.0, 10.0),
    "bike": (15.0, 2.0),
    "walk": (5.0, 0.0),
    "tram": (18.0, 12.0),  # wait + walks at both ends
}
# the ways the street map can answer for (the bus has no map data: always the rough estimate)
STREET_MODES = ("walk", "bike", "car", "tram")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def travel_minutes(straight_km: float, mode: str) -> float:
    speed, overhead = MODE_SPEED[mode]
    return straight_km * DETOUR_FACTOR / speed * 60 + overhead


def reach_km(mode: str, minutes: float) -> float:
    """Straight-line distance you can cover in `minutes` (the inverse of travel_minutes)."""
    speed, overhead = MODE_SPEED[mode]
    return max(0.0, (minutes - overhead) / 60 * speed / DETOUR_FACTOR)


__all__ = ["INF", "MODE_SPEED", "STREET_MODES", "Access", "accesses", "haversine_km", "reach_km",
           "street_minutes", "travel_minutes"]


@dataclass(frozen=True)
class Access:
    """One way to reach a job from one place."""

    place_label: str
    mode: str
    minutes: float
    max_minutes: int
    bonus_points: int
    approx: bool = True  # True = the rough straight-line estimate, False = real streets

    @property
    def reachable(self) -> bool:
        return self.minutes <= self.max_minutes


def street_minutes(streets, mode: str, origin: tuple[float, float],
                   destination: tuple[float, float]) -> float | None:
    """Minutes by real streets, INF when there is no way, None when the map cannot answer."""
    if streets is None or mode not in STREET_MODES:
        return None
    try:
        return streets.minutes(mode, origin, destination)
    except Exception:  # a map problem must never stop scoring: fall back to the rough estimate
        log.exception("street map failed for mode %s; using the rough estimate", mode)
        return None


def accesses(
    lat: float, lon: float, places: list, has_car: bool, streets=None
) -> list[Access]:
    """All (place, mode) options for a job at lat/lon.

    `places` are Place rows (label, lat, lon, max_minutes_by_mode, bonus_points, active).
    A place only offers modes listed in its max_minutes_by_mode; 'car' also needs has_car.
    `streets` is the street map (`StreetMap`) or None; a way with no route at all by that mode
    (INF) is offered with infinite minutes, so it can never count as reachable.
    """
    out: list[Access] = []
    for p in places:
        if not p.active or p.lat is None or p.lon is None:
            continue
        km = haversine_km(p.lat, p.lon, lat, lon)
        for mode, limit in (p.max_minutes_by_mode or {}).items():
            if mode not in MODE_SPEED or (mode == "car" and not has_car):
                continue
            real = street_minutes(streets, mode, (p.lat, p.lon), (lat, lon))
            if real is not None:
                out.append(Access(p.label, mode, real, int(limit), p.bonus_points, approx=False))
            else:
                out.append(Access(p.label, mode, travel_minutes(km, mode), int(limit),
                                  p.bonus_points, approx=True))
    return out
