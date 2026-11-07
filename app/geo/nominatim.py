"""Geocoding: offline town table first, then OpenStreetMap Nominatim for street addresses.

Nominatim usage policy: identify the app (User-Agent), max 1 request/second, cache results.
Only the address text is sent, never anything else about the user.
"""

import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.geo.cities import lookup_city, normalize
from app.geo.travel import haversine_km
from app.models import GeoCache

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MIN_INTERVAL_S = 1.1  # a little over the 1 req/s policy
MAX_KM_FROM_TOWN = 15.0  # a street hit farther than this from the ad's town is a wrong match
MAX_KM_FROM_TOWN_PLACE = 8.0  # a named place (a shopping centre) must lie inside the town


class Throttle:
    """At most one call per `interval` seconds, across threads."""

    def __init__(
        self,
        interval: float = MIN_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.interval, self._sleep, self._clock = interval, sleep, clock
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            if self._last is not None:
                wait = self.interval - (self._clock() - self._last)
                if wait > 0:
                    self._sleep(wait)
            self._last = self._clock()


# Nominatim's limit is per IP, so every Geocoder in the app shares this one.
SHARED_THROTTLE = Throttle()


@dataclass(frozen=True)
class GeoResult:
    lat: float
    lon: float
    precision: str  # "address" (Nominatim) or "city" (offline table)
    display_name: str | None = None


class Geocoder:
    def __init__(
        self,
        session: Session,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        throttle: Throttle | None = None,
    ):
        self.session = session
        self._owns_http = http is None
        self.http = http or httpx.Client(
            timeout=15, headers={"User-Agent": get_settings().geo_user_agent}
        )
        # tests pass their own sleep/clock and get a private throttle; the app shares one
        custom = sleep is not time.sleep or clock is not time.monotonic
        self._throttle_obj = throttle or (Throttle(MIN_INTERVAL_S, sleep, clock) if custom
                                          else SHARED_THROTTLE)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> "Geocoder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _throttle(self) -> None:
        self._throttle_obj.wait()

    def geocode_address(self, address: str) -> GeoResult | None:
        """Look up a street address (cache first, then Nominatim). Returns None if not found."""
        key = normalize(address)
        if not key:
            return None
        cached = self.session.scalar(select(GeoCache).where(GeoCache.query == key))
        if cached is not None:
            if cached.lat is None or cached.lon is None:
                return None
            return GeoResult(cached.lat, cached.lon, "address", cached.display_name)

        self._throttle()
        try:
            r = self.http.get(
                NOMINATIM_URL,
                params={"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "hr"},
            )
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("nominatim failed for %r: %s", address, e)
            return None  # do not cache failures: it may work next time

        if not data:
            self.session.add(GeoCache(query=key))
            self.session.commit()
            return None
        hit = data[0]
        res = GeoResult(float(hit["lat"]), float(hit["lon"]), "address", hit.get("display_name"))
        self.session.add(
            GeoCache(query=key, lat=res.lat, lon=res.lon, display_name=res.display_name)
        )
        self.session.commit()
        return res

    def resolve(
        self, city: str | None, address: str | None = None, place_names: Sequence[str] = ()
    ) -> GeoResult | None:
        """Best location for a job: street address if we can find it, then a named place from
        the title ("Portanova"), else the town centre (precision "city")."""
        coords = lookup_city(city)
        if address:
            full = f"{address}, {city}" if city and city.lower() not in address.lower() else address
            res = self.geocode_address(full)
            if res and coords and haversine_km(res.lat, res.lon, *coords) > MAX_KM_FROM_TOWN:
                log.info("street hit for %r is far from %s, using town centre", full, city)
                res = None  # e.g. same street name in a different town
            if res:
                return res
        if coords and city:  # a name alone ("Portanova") means nothing without the town
            tried: set[str] = set()
            for name in place_names:
                bare = re.sub(re.escape(city), "", name, flags=re.IGNORECASE).strip(" ,-")
                if not bare or bare in tried or lookup_city(bare) is not None:
                    continue  # nothing left, searched already, or a town/district, not a place
                tried.add(bare)
                full = f"{bare}, {city}"
                res = self.geocode_address(full)
                if res and haversine_km(res.lat, res.lon, *coords) <= MAX_KM_FROM_TOWN_PLACE:
                    return res
                if res:
                    log.info("hit for %r is not inside %s, ignoring it", full, city)
        if coords:
            return GeoResult(coords[0], coords[1], "city")
        return None
