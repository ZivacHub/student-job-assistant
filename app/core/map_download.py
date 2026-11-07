"""The "Preuzmi kartu ulica" button: download in the background, then score every ad again.

One download at a time. The page reads `STATE` to show what is going on. Everything the user can
see is in plain Croatian; the technical detail goes to the log.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from app.core.places import list_places
from app.core.profile import get_or_create_profile
from app.geo import mapstore
from app.geo.bus_match import report_bus_lines
from app.geo.osm_download import MapDownloadError, download_all
from app.models import Event
from app.models.base import utcnow
from app.scoring.rescore import rescore_jobs

log = logging.getLogger(__name__)


@dataclass
class DownloadState:
    running: bool = False
    step: str = ""  # what is being fetched right now, for the page
    error: str | None = None  # plain Croatian, shown in the red box
    finished_at: datetime | None = None
    size_bytes: int = 0
    rescored: int | None = None


STATE = DownloadState()
_lock = threading.Lock()


def snapshot() -> DownloadState:
    with _lock:
        return DownloadState(**vars(STATE))


def _set(**changes) -> None:
    with _lock:
        for key, value in changes.items():
            setattr(STATE, key, value)


def reset_state() -> None:
    """For tests: forget the last result."""
    _set(running=False, step="", error=None, finished_at=None, size_bytes=0, rescored=None)


def run_download(
    session_factory: Callable,
    data_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Download every map area, then score the ads again with it. Never raises."""
    try:
        size = download_all(data_dir, client=client, sleep=sleep,
                            progress=lambda text: _set(step=text))
        _set(step="računam ocjene oglasa")
        with session_factory() as session:
            profile = get_or_create_profile(session)
            places = [p for p in list_places(session) if p.active]
            streets = mapstore.get_street_map(data_dir)
            count = rescore_jobs(session, profile, places, streets=streets)
            bus = getattr(streets, "bus", None)
            session.add(Event(kind="map_downloaded", data={
                "bytes": size, "rescored": count, "bus_lines": bus.lines if bus else []}))
            _report_bus_lines(session, streets)
            session.commit()
        _set(size_bytes=size, rescored=count, error=None, finished_at=utcnow())
    except MapDownloadError as exc:
        _fail(session_factory, str(exc))
    except Exception:  # whatever happens, the page must get a plain message, not a stack trace
        log.exception("map download failed")
        _fail(session_factory, "Preuzimanje karte nije uspjelo zbog neočekivane greške. "
                               "Pogledajte Dnevnik ili pokušajte ponovno.")
    finally:
        _set(running=False, step="")


def _report_bus_lines(session, streets) -> None:
    """Say how the loaded bus timetable fits the new map. Never makes a good download fail."""
    if streets is None:
        return
    try:
        report_bus_lines(session, streets)
    except Exception:
        log.exception("could not match the bus timetable to the map")


def _fail(session_factory: Callable, message: str) -> None:
    _set(error=message, finished_at=utcnow())
    try:
        with session_factory() as session:
            session.add(Event(kind="map_download_failed", data={"error": message}))
            session.commit()
    except Exception:
        log.exception("could not log the failed map download")


def start_download(session_factory: Callable, data_dir: Path | None = None,
                   **kwargs) -> bool:
    """Start the download in the background. False if one is already running."""
    with _lock:
        if STATE.running:
            return False
        STATE.running, STATE.error, STATE.step = True, None, "priprema"
    threading.Thread(target=run_download, args=(session_factory, data_dir), kwargs=kwargs,
                     daemon=True, name="map-download").start()
    return True
