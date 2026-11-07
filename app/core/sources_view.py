"""The Sources page (plan 4.4, 2.8): how each source of ads is doing, and what Ivan may change.

Reads the Source rows that every collection run keeps up to date (`collect()` in
app/sources/ingest.py). Changing a source writes an Event and, when a scheduler is running, makes
it apply the new schedule at once. A source is never contacted from here except through the
scheduler's own polite run (`run_source`), started in the background.

Words: "izvor" = a website (or channel) the app collects ads from; "provjera" = one collection
pass over it; "prvo punjenje" = the first pass, when the ads that already exist are saved without
alerts.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.inbox import describe_age, source_name
from app.models import Event, Job, Source
from app.models.base import utcnow
from app.sources.ingest import MAX_BLOCKED_BEFORE_DISABLE

INTERVAL_CHOICES = (5, 10, 15, 30, 60, 180)
RECENT = timedelta(hours=24)
MIN_GAP = timedelta(seconds=60)  # a source checked a moment ago is not asked again
MAX_ERROR_CHARS = 300

# last_status -> (css state, short label, plain explanation)
STATUS_TEXT = {
    "ok": ("ok", "U redu", None),
    "partial": ("warn", "Djelomično",
                "Neki oglasi nisu učitani. Aplikacija će pokušati ponovno."),
    "error": ("warn", "Greška",
              "Zadnja provjera nije uspjela (stranica nije odgovorila ili je vratila grešku). "
              "Aplikacija pokušava ponovno."),
    "blocked": ("bad", "Pristup odbijen",
                "Stranica je odbila pristup. Aplikacija ne pokušava zaobići zabranu."),
    "parse_error": ("bad", "Izvor izgleda pokvaren",
                    "Stranica izgleda drugačije nego prije pa se oglasi ne mogu pročitati. "
                    "Čitač za taj izvor treba popraviti."),
}
SERIOUS = ("blocked", "parse_error")  # shown as a warning on the Inbox at once


class SettingsError(ValueError):
    """The request makes no sense (unknown source, interval not offered). Message is Croatian."""


@dataclass
class SourceRow:
    key: str
    name: str
    enabled: bool
    interval: int
    manual: bool  # pasted ads: never polled, nothing to run or schedule
    state: str  # ok / warn / bad / off / new / run (a css class)
    state_label: str
    explanation: str | None
    runnable: bool  # a checker exists for it in this run of the program
    last_run: str | None
    last_items: int
    total_ads: int
    ads_24h: int
    error: str | None
    failures: int
    running: bool
    next_run: str | None
    first_fill_pending: bool
    auto_disabled: bool


def describe_future(then: datetime | None, now: datetime) -> str | None:
    """'za 7 min', 'za 2 sata' (no plural worries: minutes, then hours)."""
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    minutes = int((then - now).total_seconds() // 60)
    if minutes < 1:
        return "za manje od minute"
    if minutes < 60:
        return f"za {minutes} min"
    return f"za {minutes // 60} h"


def _clip(text: str | None) -> str | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS].rstrip() + " …"


def is_auto_disabled(source: Source) -> bool:
    return (not source.enabled and source.last_status == "blocked"
            and source.consecutive_failures >= MAX_BLOCKED_BEFORE_DISABLE)


def build_row(source: Source, totals: tuple[int, int], scheduler, now: datetime) -> SourceRow:
    manual = source.interval_minutes <= 0
    running = bool(scheduler is not None and scheduler.is_running(source.key))
    auto_off = is_auto_disabled(source)
    state, label, explanation = STATUS_TEXT.get(source.last_status or "", ("new", "", None))
    if running:
        state, label, explanation = "run", "Provjera u tijeku", None
    elif not source.enabled:
        state, label = "off", "Isključeno"
        explanation = (f"Isključeno jer je stranica {MAX_BLOCKED_BEFORE_DISABLE} puta odbila "
                       "pristup. Možete ga ponovno uključiti." if auto_off else None)
    elif source.last_status is None:
        state, label = ("ok", "Ručno dodavanje") if manual else ("new", "Još nije pokrenuto")
    has_checker = manual or scheduler is None or _knows(scheduler, source.key)
    if not has_checker:
        state, label = "new", "Nema čitača"
        explanation = ("Ova verzija programa nema čitač za taj izvor, pa se s njega sami ne "
                       "dohvaćaju oglasi. Oglase možete dodati ručno ili (kasnije) preko "
                       "obavijesti e-poštom.")
    next_run = None
    if scheduler is not None and has_checker and source.enabled and not manual and not running:
        next_run = describe_future(scheduler.next_run(source.key), now)
    return SourceRow(
        key=source.key,
        name=source_name(source),
        enabled=source.enabled,
        interval=source.interval_minutes,
        manual=manual,
        state=state,
        state_label=label,
        explanation=explanation,
        runnable=has_checker,
        last_run=describe_age(source.last_run_at, now),
        last_items=source.last_items or 0,
        total_ads=totals[0],
        ads_24h=totals[1],
        error=_clip(source.last_error) if source.last_status not in (None, "ok") else None,
        failures=source.consecutive_failures or 0,
        running=running,
        next_run=next_run,
        first_fill_pending=(not manual and source.enabled and source.baselined_at is None
                            and source.last_status is not None),
        auto_disabled=auto_off,
    )


def _ad_counts(session: Session, since: datetime) -> dict[int, tuple[int, int]]:
    rows = session.execute(
        select(
            Job.source_id,
            func.count(),
            func.sum(case((Job.first_seen_at >= since, 1), else_=0)),
        ).where(Job.duplicate_of.is_(None)).group_by(Job.source_id)
    ).all()
    return {source_id: (total, int(recent or 0)) for source_id, total, recent in rows}


def load_sources(session: Session, scheduler=None, now: datetime | None = None) -> list[SourceRow]:
    now = now or utcnow()
    counts = _ad_counts(session, now - RECENT)
    sources = session.scalars(select(Source).order_by(Source.id)).all()
    return [build_row(s, counts.get(s.id, (0, 0)), scheduler, now) for s in sources]


def broken_sources(session: Session, scheduler=None) -> list[str]:
    """Names of sources Ivan should hear about on the Inbox: serious trouble, or two failures
    in a row (a single failed run is usually the network and fixes itself). A source this
    program has no checker for is not "broken": it is simply not checked."""
    names = []
    for s in session.scalars(select(Source).order_by(Source.id)):
        if s.interval_minutes <= 0 or (scheduler is not None and not _knows(scheduler, s.key)):
            continue
        if is_auto_disabled(s):
            names.append(source_name(s))
        elif s.enabled and (
            s.last_status in SERIOUS
            or (s.last_status in ("error", "partial") and s.consecutive_failures >= 2)
        ):
            names.append(source_name(s))
    return names


def save_source_settings(
    session: Session, key: str, *, enabled: bool, interval: int, now: datetime | None = None
) -> Source:
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None:
        raise SettingsError("Taj izvor ne postoji.")
    if source.interval_minutes <= 0:
        raise SettingsError("Ručno dodani oglasi nemaju postavki.")
    if interval not in INTERVAL_CHOICES:
        raise SettingsError("Taj razmak između provjera nije ponuđen.")
    was_enabled = source.enabled
    source.enabled, source.interval_minutes = enabled, interval
    if enabled and not was_enabled:
        source.consecutive_failures = 0  # a fresh start after Ivan switched it back on
    session.add(Event(kind="source_changed", data={
        "source": key, "enabled": enabled, "interval": interval, "by": "user"}))
    session.flush()
    return source


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive UTC


def _knows(scheduler, key: str) -> bool:
    """Does the scheduler have a checker for this source? (A stand-in without `knows` has all.)"""
    knows = getattr(scheduler, "knows", None)
    return True if knows is None else bool(knows(key))


def start_run(session: Session, scheduler, key: str, now: datetime | None = None) -> str:
    """Start one polite collection pass in the background. Returns what happened:
    'started', 'unknown', 'no_collector' (this program has no checker for it), 'off' (switched
    off), 'busy' (already running), 'recent' (checked less than a minute ago: never hammer a
    website), 'no_scheduler'."""
    now = now or utcnow()
    source = session.scalar(select(Source).where(Source.key == key))
    if source is None or source.interval_minutes <= 0:
        return "unknown"
    if scheduler is None or not hasattr(scheduler, "run_source"):
        return "no_scheduler"
    if not _knows(scheduler, key):
        return "no_collector"
    if not source.enabled:
        return "off"
    if scheduler.is_running(key):
        return "busy"
    if source.last_run_at is not None and now - _aware(source.last_run_at) < MIN_GAP:
        return "recent"
    session.add(Event(kind="source_run_requested", data={"source": key, "by": "user"}))
    session.flush()
    threading.Thread(target=scheduler.run_source, args=(key,), daemon=True,
                     name=f"run-now-{key}").start()
    return "started"


def any_running(rows: list[SourceRow]) -> bool:
    return any(r.running for r in rows)


# ---------------------------------------------------------------- the refresh button on the Inbox
@dataclass
class RefreshResult:
    started: list[str]  # names of the sources that were asked to check now
    recent: list[str]  # skipped: checked less than a minute ago
    busy: list[str]  # skipped: already checking
    no_scheduler: bool = False  # automatic checking is not on in this run of the program
    no_sources: bool = False


def polled_sources(session: Session) -> list[Source]:
    return [s for s in session.scalars(select(Source).order_by(Source.id))
            if s.interval_minutes > 0 and s.enabled]


def refresh_all(session: Session, scheduler, now: datetime | None = None) -> RefreshResult:
    """Ask every enabled source to check for new ads now (each one politely, in the background)."""
    now = now or utcnow()
    sources = [s for s in polled_sources(session)
               if scheduler is None or _knows(scheduler, s.key)]  # nothing to ask without a checker
    result = RefreshResult(started=[], recent=[], busy=[], no_sources=not sources)
    for source in sources:
        outcome = start_run(session, scheduler, source.key, now)
        name = source_name(source)
        if outcome == "started":
            result.started.append(name)
        elif outcome == "recent":
            result.recent.append(name)
        elif outcome == "busy":
            result.busy.append(name)
        elif outcome == "no_scheduler":
            result.no_scheduler = True
            break
    return result


@dataclass
class RefreshState:
    running: bool  # some source is checking right now
    last_check: str | None  # 'prije 4 min': the newest check of any enabled source
    new_ads: int | None  # open ads first seen since the refresh began (None: not asked)


def new_ads_phrase(n: int) -> str:
    """'1 novi oglas', '3 nova oglasa', '5 novih oglasa'."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} novi oglas"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} nova oglasa"
    return f"{n} novih oglasa"


def refresh_state(
    session: Session, scheduler, since: datetime | None = None, now: datetime | None = None
) -> RefreshState:
    now = now or utcnow()
    sources = polled_sources(session)
    if scheduler is not None:
        sources = [s for s in sources if _knows(scheduler, s.key)]
    running = bool(scheduler is not None and any(scheduler.is_running(s.key) for s in sources))
    last = max((_aware(s.last_run_at) for s in sources if s.last_run_at), default=None)
    new_ads = None
    if since is not None:
        new_ads = session.scalar(
            select(func.count()).select_from(Job).where(
                Job.duplicate_of.is_(None), Job.first_seen_at >= since,
                Job.status.in_(("new", "seen")))
        ) or 0
    return RefreshState(running=running, last_check=describe_age(last, now), new_ads=new_ads)


def parse_since(raw: str | None, now: datetime | None = None) -> datetime | None:
    """The moment a refresh began, from the address ('od' = seconds since 1970). Anything odd,
    from the future or older than a day is ignored."""
    now = now or utcnow()
    try:
        seconds = int((raw or "").strip())
        when = datetime.fromtimestamp(seconds, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    if when > now + timedelta(minutes=1) or now - when > timedelta(days=1):
        return None
    return when
