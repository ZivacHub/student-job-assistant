"""The one-time download of street maps from OpenStreetMap (plan step 4.9, D27).

Privacy: a request carries only the question "which streets are inside this map area" (the box of
the area from `mapstore.AREAS`). No place of Ivan's, no job, no address is ever part of it; this
module does not even know them.

The public Overpass servers are sometimes at capacity (a 502 answer), so each part is tried on
several servers with growing pauses. A file is written only when the answer is complete and
looks like a real map, and it is written atomically (the old map stays until the new one is whole).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.core.settings import get_settings
from app.geo import mapstore
from app.geo.mapstore import Area

SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
PAUSES_S = (3.0, 10.0, 30.0, 60.0)  # waits between rounds of attempts
RETRY_STATUS = {429, 500, 502, 503, 504}
MIN_WAYS = {"detailed": 500, "main_roads": 100}  # fewer than this is not a real map of the area
TIMEOUT = httpx.Timeout(330.0, connect=20.0)


class MapDownloadError(Exception):
    """A download that failed, with a message in plain Croatian for the page."""


def build_queries(area: Area) -> list[tuple[str, str]]:
    """The questions to ask for one area: [(what it fetches, Overpass query)]."""
    s, w, n, e = area.bbox
    box = f"{s},{w},{n},{e}"
    if area.kind == "detailed":
        return [
            ("ulice i staze", f'[out:json][timeout:240];way["highway"]({box});out geom;'),
            ("semafori i pješački prijelazi",
             "[out:json][timeout:120];("
             f'node["highway"="traffic_signals"]({box});node["highway"="crossing"]({box});'
             f'node["crossing"="traffic_signals"]({box});node["railway"="level_crossing"]({box});'
             ");out;"),
            ("linije tramvaja",
             f'[out:json][timeout:60];relation["route"="tram"]({box});out geom;'),
            # bus routes: the relations with their members in order, then the stop nodes they
            # name (a bus line has hundreds of street pieces; only the stops are wanted)
            ("linije autobusa",
             f'[out:json][timeout:90];relation["route"="bus"]({box})->.r;.r out body;'
             "node(r.r)->.n;.n out;"),
        ]
    return [("glavne ceste", "[out:json][timeout:240];way["
            '"highway"~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"]'
            f"({box});out geom;")]


def _fetch(client: httpx.Client, query: str, sleep: Callable[[float], None]) -> dict:
    """One query, tried on every server, with pauses, until a complete answer arrives."""
    last = "nepoznata greška"
    for round_no, pause in enumerate((0.0, *PAUSES_S)):
        if pause:
            sleep(pause)
        server = SERVERS[round_no % len(SERVERS)]
        try:
            response = client.post(server, data={"data": query})
        except httpx.HTTPError as exc:
            last = f"nema veze s poslužiteljem karata ({type(exc).__name__})"
            continue
        if response.status_code in RETRY_STATUS:
            last = "poslužitelj karata je trenutno preopterećen"
            continue
        if response.status_code != 200:
            raise MapDownloadError(
                f"Poslužitelj karata je odgovorio greškom {response.status_code}.")
        try:
            doc = response.json()
        except ValueError:
            last = "odgovor poslužitelja nije bio ispravan"
            continue
        remark = str(doc.get("remark", "")) if isinstance(doc, dict) else ""
        if not isinstance(doc, dict) or not isinstance(doc.get("elements"), list) or (
                "error" in remark.lower() or "timed out" in remark.lower()):
            last = "poslužitelj nije uspio dovršiti odgovor"
            continue
        return doc
    raise MapDownloadError(
        f"Karta se nije mogla preuzeti ({last}). Pokušajte ponovno za nekoliko minuta.")


def download_area(
    area: Area,
    data_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Download one area and save it. Returns the size of the saved file in bytes."""
    own = client is None
    http = client or httpx.Client(
        timeout=TIMEOUT, headers={"User-Agent": get_settings().geo_user_agent})
    try:
        elements: list[dict] = []
        queries = build_queries(area)
        for number, (what, query) in enumerate(queries, 1):
            if progress:
                progress(f"{area.label.split(':')[0]}: {what} ({number}/{len(queries)})")
            elements.extend(_fetch(http, query, sleep)["elements"])
    finally:
        if own:
            http.close()
    ways = sum(1 for e in elements if e.get("type") == "way")
    if ways < MIN_WAYS[area.kind]:
        raise MapDownloadError(
            "Preuzeta karta je prazna ili nepotpuna, pa nije spremljena. "
            "Pokušajte ponovno za nekoliko minuta.")
    return _save(area, elements, data_dir)


def _save(area: Area, elements: list[dict], data_dir: Path | None) -> int:
    target = mapstore.area_file(area, data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".json.part")
    payload = json.dumps({"elements": elements}, ensure_ascii=False, separators=(",", ":"))
    partial.write_text(payload, encoding="utf-8")
    json.loads(partial.read_text(encoding="utf-8"))  # whole and readable, or nothing is replaced
    os.replace(partial, target)
    meta = {"downloaded_at": datetime.now(UTC).isoformat(), "bytes": target.stat().st_size,
            "ways": sum(1 for e in elements if e.get("type") == "way"),
            "bus_routes": sum(1 for e in elements if e.get("type") == "relation"
                              and (e.get("tags") or {}).get("route") == "bus"),
            "bbox": list(area.bbox), "source": "OpenStreetMap contributors, ODbL"}
    mapstore.meta_file(area, data_dir).write_text(json.dumps(meta), encoding="utf-8")
    return target.stat().st_size


def download_all(
    data_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Download every area. Returns the total size. The map in memory is dropped afterwards."""
    total = 0
    try:
        for area in mapstore.AREAS:
            total += download_area(area, data_dir, client=client, sleep=sleep, progress=progress)
    finally:
        mapstore.forget_street_map()
    return total
