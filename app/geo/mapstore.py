"""The street map files on this PC (plan step 4.9, D27): which areas, where they live, loading.

The files come from the one-time download in `osm_download.py`. They are kept in
`data/maps/` (never in git) as OpenStreetMap data, (c) OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.settings import get_settings
from app.geo.streets import StreetMap


@dataclass(frozen=True)
class Area:
    key: str
    label: str  # in Croatian, for the page
    bbox: tuple[float, float, float, float]  # south, west, north, east
    kind: str  # "detailed": every street, path, light and the tram; "main_roads": for driving far


# Osijek and the villages around it in every detail (walking, cycling, tram, local driving),
# and the main roads of the wider region (so a car can reach Đakovo, Valpovo, Našice ...).
# A new city is a new row here, nothing else.
AREAS: tuple[Area, ...] = (
    Area("osijek-grad", "Osijek i okolna naselja: sve ulice, staze, semafori i tramvaj",
         (45.45, 18.50, 45.65, 18.85), "detailed"),
    Area("osijek-regija", "Šira okolica Osijeka: glavne ceste za vožnju autom",
         (45.20, 17.90, 45.95, 19.35), "main_roads"),
)
AREA_BY_KEY = {a.key: a for a in AREAS}


def maps_dir(data_dir: Path | None = None) -> Path:
    return (data_dir or get_settings().data_dir) / "maps"


def area_file(area: Area, data_dir: Path | None = None) -> Path:
    return maps_dir(data_dir) / f"{area.key}.json"


def meta_file(area: Area, data_dir: Path | None = None) -> Path:
    return maps_dir(data_dir) / f"{area.key}.meta.json"


@dataclass(frozen=True)
class AreaStatus:
    area: Area
    present: bool
    size_bytes: int
    downloaded_at: datetime | None
    bus_routes: int | None = None  # None: downloaded before bus lines were part of the map (4.12)


def status(data_dir: Path | None = None) -> list[AreaStatus]:
    out = []
    for area in AREAS:
        path = area_file(area, data_dir)
        present = path.is_file()
        when, buses = None, None
        if present:
            try:
                meta = json.loads(meta_file(area, data_dir).read_text(encoding="utf-8"))
                when = datetime.fromisoformat(meta["downloaded_at"])
                count = meta.get("bus_routes")
                buses = count if isinstance(count, int) else None
            except (OSError, ValueError, KeyError):
                when = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        out.append(AreaStatus(area, present, path.stat().st_size if present else 0, when, buses))
    return out


# ---------------------------------------------------------------- the map in memory
_lock = threading.Lock()
_cache: tuple[tuple, StreetMap | None] | None = None


def _signature(data_dir: Path | None) -> tuple:
    files = []
    for area in AREAS:
        path = area_file(area, data_dir)
        if path.is_file():
            st = path.stat()
            files.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(files)


def get_street_map(data_dir: Path | None = None) -> StreetMap | None:
    """The street map built from the downloaded files, or None when nothing was downloaded.

    Built once per process (a few seconds) and rebuilt only when a file changes. A file that
    cannot be read is skipped: the app then falls back to the rough estimate instead of failing.
    """
    global _cache
    signature = (str(data_dir or get_settings().data_dir), _signature(data_dir))
    with _lock:
        if _cache is not None and _cache[0] == signature:
            return _cache[1]
        documents = []
        for area in AREAS:
            path = area_file(area, data_dir)
            if not path.is_file():
                continue
            try:
                documents.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        built = StreetMap.from_osm(documents) if documents else None
        _cache = (signature, built)
        return built


def forget_street_map() -> None:
    """Drop the map from memory (after a download or a deleted file)."""
    global _cache
    with _lock:
        _cache = None
