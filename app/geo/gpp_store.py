"""Reads GPP Osijek's timetables into the database, politely (plan 4.11, D41).

Rules, all from the project's scraping rules and GPP's published terms (checked 2026-11-04: their
terms of use allow copying for informational, non-commercial use with a copyright notice and say
nothing about automatic reading; their robots.txt allows everything and sets no crawl delay):
- at most once a day per page, counting a failed try too, so a broken page is never hammered;
- through `PoliteClient`: robots.txt first, the program's own User-Agent, 10 seconds between
  requests, no evasion of a refusal;
- the PDFs are never fetched, only the two HTML pages;
- a page that does not look as expected, or any failure, leaves the data from before untouched
  and is written to the log; a new version replaces the old one in one step.
Where the times are shown, `ATTRIBUTION` goes with them (the terms ask for a copyright notice).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.geo.bus_match import report_bus_lines
from app.geo.gpp_parse import BASE_URL, Timetable, content_hash, parse_timetable
from app.models import Event, TimetableBoard, TimetableDeparture, TimetableSource
from app.models.base import utcnow
from app.sources.http import SourceBlocked, SourceError, SourceParseError

log = logging.getLogger(__name__)

PAGES = {"tram": "/polasci-tramvaj/", "bus": "/polasci-autobus/"}
ATTRIBUTION = "© Gradski prijevoz putnika d.o.o. Osijek (GPP Osijek), web.gpp-osijek.com"
REFRESH_EVERY = timedelta(hours=24)
MAX_PAGE_CHARS = 5_000_000  # the real pages are about 0.3 and 0.85 million characters

LOADED = "loaded"
UNCHANGED = "unchanged"
NOT_DUE = "not_due"
PARSE_ERROR = "parse_error"
ERROR = "error"
BLOCKED = "blocked"


class Client(Protocol):
    def get(self, url: str): ...


ClientFactory = Callable[[], "Client"]


@dataclass
class RefreshResult:
    mode: str
    status: str  # LOADED, UNCHANGED, NOT_DUE, PARSE_ERROR, ERROR or BLOCKED
    boards: int = 0
    departures: int = 0
    message: str | None = None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive UTC


def source_row(session: Session, mode: str) -> TimetableSource:
    row = session.scalar(select(TimetableSource).where(TimetableSource.mode == mode))
    if row is None:
        row = TimetableSource(mode=mode, boards=0, departures=0)
        session.add(row)
        session.flush()
    return row


def is_due(row: TimetableSource, now: datetime) -> bool:
    return row.last_attempt_at is None or now - _aware(row.last_attempt_at) >= REFRESH_EVERY


def replace_timetable(session: Session, timetable: Timetable) -> None:
    """The new boards take the place of the old ones of that mode, all in one step."""
    old = select(TimetableBoard.id).where(TimetableBoard.mode == timetable.mode)
    session.execute(delete(TimetableDeparture).where(TimetableDeparture.board_id.in_(old)))
    session.execute(delete(TimetableBoard).where(TimetableBoard.mode == timetable.mode))
    for board in timetable.boards:
        session.add(TimetableBoard(
            mode=board.mode, title=board.title, route=board.route, stop=board.stop,
            tolerance_min=board.tolerance_min, pdf_url=board.pdf_url,
            valid_label=board.valid_label, notes=board.notes,
            departures=[TimetableDeparture(day_type=d.day_type, minute=d.minute, suffix=d.suffix)
                        for d in board.departures]))
    session.flush()


def _failed(session: Session, row: TimetableSource, status: str, message: str,
            now: datetime) -> RefreshResult:
    row.last_status, row.last_error = status, message[:500]
    session.add(Event(kind="timetable_failed", ts=now, data={"mode": row.mode, "status": status}))
    session.commit()
    log.warning("timetable %s not loaded (%s): %s", row.mode, status, message)
    return RefreshResult(row.mode, status, row.boards, row.departures, message)


def _report_bus_lines(session: Session) -> None:
    """After a new bus timetable: say how it fits the map's bus routes (step 4.12). A problem
    here never turns a good load into a failed one."""
    try:
        report_bus_lines(session)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("could not match the bus timetable to the map")


def refresh(session: Session, mode: str, client: Client, *, now: datetime | None = None,
            force: bool = False) -> RefreshResult:
    """Read one page. Never raises for a site problem: the old data stays and the result says why
    nothing changed. `force` skips the once-a-day rule (Ivan's own button, step 4.14)."""
    now = now or utcnow()
    row = source_row(session, mode)
    if not force and not is_due(row, now):
        return RefreshResult(mode, NOT_DUE, row.boards, row.departures)
    row.last_attempt_at = now  # counted before the request: a failing page is not asked again today
    session.commit()
    try:
        response = client.get(BASE_URL + PAGES[mode])
    except SourceBlocked as error:
        return _failed(session, row, BLOCKED, str(error), now)
    except SourceError as error:  # network, robots.txt, 404, 5xx
        return _failed(session, row, ERROR, str(error), now)
    try:
        html = response.text
        if len(html) > MAX_PAGE_CHARS:
            raise SourceParseError("the page is far larger than expected")
        timetable = parse_timetable(html, mode)
    except SourceParseError as error:
        return _failed(session, row, PARSE_ERROR, str(error), now)

    digest = content_hash(timetable)
    row.last_success_at, row.last_error = now, None
    if digest == row.content_hash:
        row.last_status = UNCHANGED
        session.commit()
        return RefreshResult(mode, UNCHANGED, row.boards, row.departures)
    replace_timetable(session, timetable)
    row.content_hash, row.valid_label = digest, timetable.valid_label
    row.boards, row.departures = len(timetable.boards), timetable.departures
    row.last_status = LOADED
    session.add(Event(kind="timetable_loaded", ts=now, data={
        "mode": mode, "boards": row.boards, "departures": row.departures,
        "valid": timetable.valid_label, "skipped": len(timetable.skipped)}))
    session.commit()
    if mode == "bus":
        _report_bus_lines(session)
    return RefreshResult(mode, LOADED, row.boards, row.departures)


def refresh_all(session: Session, *, client_factory: ClientFactory, now: datetime | None = None,
                force: bool = False) -> list[RefreshResult]:
    """Both pages, one after the other (the client's own pause keeps them 10 seconds apart). The
    client is only made when at least one page is due, and it must be given: there is no default,
    so a test can never reach the real site by forgetting it."""
    now = now or utcnow()
    due = [m for m in PAGES if force or is_due(source_row(session, m), now)]
    results = []
    for mode in PAGES:
        if mode not in due:
            row = source_row(session, mode)
            results.append(RefreshResult(mode, NOT_DUE, row.boards, row.departures))
    if not due:
        return results
    client = client_factory()
    try:
        results += [refresh(session, mode, client, now=now, force=force) for mode in due]
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
    return results
