"""Polls every enabled job source on its own interval while the app runs (plan step 3.1).

- One job per source, at most one run of a source at a time (APScheduler `max_instances=1` plus a
  per-source lock, so a manual "run now" can never overlap a scheduled run).
- Sources can be switched off in the database (`Source.enabled`) while the app runs: the next
  run notices. A source with `interval_minutes == 0` (pasted ads) is never scheduled.
- After a restart a source is not polled again before its interval has passed since its last run.
- `collect()` records every problem on the Source row, so a failing site never stops the others.
"""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.profile import get_or_create_profile
from app.geo.nominatim import Geocoder
from app.models import Place, Source
from app.models.base import utcnow
from app.sources.base import BaseSource
from app.sources.http import PoliteClient
from app.sources.ingest import collect, get_or_create_source
from app.sources.studentski_poslovi import StudentskiPoslovi

log = logging.getLogger(__name__)


def default_sources() -> dict[str, BaseSource]:
    """The collectors that poll on a schedule. (Paste-in and Gmail alerts are not polled here.)"""
    sources: list[BaseSource] = [StudentskiPoslovi(PoliteClient(min_interval_s=10.0))]
    return {s.key: s for s in sources}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive UTC


class Scheduler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        sources: dict[str, BaseSource],
        *,
        startup_delay_s: float = 5.0,
        geocoder_factory: Callable[[Session], Geocoder] = Geocoder,
        notifier: Callable[[], object] | None = None,
        applier: Callable[[], object] | None = None,
        locator: Callable[[], object] | None = None,
        reply_watcher: Callable[[], object] | None = None,
    ):
        self.session_factory = session_factory
        self.sources = sources
        self.startup_delay_s = startup_delay_s
        self.geocoder_factory = geocoder_factory
        self.notifier = notifier  # sends the alerts the rules decide (app/notify/dispatch.py)
        self._notify_lock = threading.Lock()
        self.applier = applier  # sends the approved applications that are due (app/apply/runner.py)
        self._apply_lock = threading.Lock()
        self.locator = locator  # checks how exact old ads' locations are (app/geo/recheck.py)
        self._locate_lock = threading.Lock()
        self.reply_watcher = reply_watcher  # reads Ivan's "DA" replies (app/apply/replyflow.py)
        self._reply_lock = threading.Lock()
        self._locks ={key: threading.Lock() for key in sources}
        self._scheduler = BackgroundScheduler(
            timezone="UTC",
            executors={"default": ThreadPoolExecutor(max(2, len(sources)))},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        )

    # ---- lifecycle
    def start(self) -> None:
        with self.session_factory() as session:
            for key, src in self.sources.items():
                row = get_or_create_source(session, src)
                if not row.enabled or row.interval_minutes <= 0:
                    log.info("not scheduling %s (enabled=%s, every %s min)",
                             key, row.enabled, row.interval_minutes)
                    continue
                self._scheduler.add_job(
                    self.run_source, "interval", minutes=row.interval_minutes, id=key,
                    args=[key], next_run_time=self.first_run_time(row), replace_existing=True,
                )
        if self.notifier is not None:  # also once a minute: a retry after a failed send, digests
            self._scheduler.add_job(
                self.notify_tick, "interval", minutes=1, id="notify",
                next_run_time=utcnow() + timedelta(seconds=self.startup_delay_s + 10),
                replace_existing=True,
            )
        if self.applier is not None:  # approved drafts go out once the undo window is over
            self._scheduler.add_job(
                self.apply_tick, "interval", minutes=1, id="apply",
                next_run_time=utcnow() + timedelta(seconds=self.startup_delay_s + 20),
                replace_existing=True,
            )
        if self.reply_watcher is not None:  # connects to Gmail only while an alert waits for DA
            self._scheduler.add_job(
                self.reply_tick, "interval", minutes=1, id="replies",
                next_run_time=utcnow() + timedelta(seconds=self.startup_delay_s + 40),
                replace_existing=True,
            )
        if self.locator is not None:  # old ads (before step 4.10): a batch per run till none left
            self._scheduler.add_job(
                self.locate_tick, "interval", minutes=5, id="locate",
                next_run_time=utcnow() + timedelta(seconds=self.startup_delay_s + 30),
                replace_existing=True,
            )
        self._scheduler.start()
        log.info("scheduler started: %s", ", ".join(self.scheduled_keys()) or "nothing to poll")

    def shutdown(self, wait: bool = False) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
        for src in self.sources.values():
            try:
                src.close()
            except Exception:  # closing must never block shutdown
                log.exception("closing %s failed", src.key)
        log.info("scheduler stopped")

    @property
    def running(self) -> bool:
        return self._scheduler.running

    def scheduled_keys(self) -> list[str]:
        return sorted(job.id for job in self._scheduler.get_jobs())

    def knows(self, key: str) -> bool:
        """True when this program has a checker (collector) for the source."""
        return key in self.sources

    def is_running(self, key: str) -> bool:
        """True while a run of this source is in progress (scheduled or started by hand)."""
        lock = self._locks.get(key)
        return bool(lock and lock.locked())

    def next_run(self, key: str) -> datetime | None:
        """When the next scheduled run happens, or None (not scheduled, or not started)."""
        if not self._scheduler.running:
            return None
        job = self._scheduler.get_job(key)
        return job.next_run_time if job is not None else None

    def sync_source(self, key: str) -> None:
        """Make the schedule match the Source row after Ivan changed it on the Sources page."""
        if key not in self.sources or not self._scheduler.running:
            return
        with self.session_factory() as session:
            row = get_or_create_source(session, self.sources[key])
            wanted = row.enabled and row.interval_minutes > 0
            next_time = self.first_run_time(row) if wanted else None
            interval = row.interval_minutes
        if not wanted:
            if self._scheduler.get_job(key) is not None:
                self._scheduler.remove_job(key)
            return
        self._scheduler.add_job(
            self.run_source, "interval", minutes=interval, id=key, args=[key],
            next_run_time=next_time, replace_existing=True,
        )

    # ---- timing
    def first_run_time(self, row: Source, now: datetime | None = None) -> datetime:
        """Soon after start, but not before the interval since the last run has passed."""
        now = now or utcnow()
        earliest = now + timedelta(seconds=self.startup_delay_s)
        if row.last_run_at is None:
            return earliest
        due = _aware(row.last_run_at) + timedelta(minutes=row.interval_minutes)
        return max(earliest, due)

    # ---- alerts
    def notify_tick(self) -> str:
        """Send the alerts that are due. Never overlaps itself, never raises: a mail problem must
        not stop the polling of the sources. Returns 'none', 'busy', 'ran' or 'error'."""
        if self.notifier is None:
            return "none"
        if not self._notify_lock.acquire(blocking=False):
            return "busy"
        try:
            self.notifier()
            return "ran"
        except Exception:
            log.exception("sending alerts failed")
            return "error"
        finally:
            self._notify_lock.release()

    def apply_tick(self) -> str:
        """Send the approved applications that are due. Never overlaps itself, never raises: a
        problem here must not stop the polling of the sources or the alerts. Returns 'none',
        'busy', 'ran' or 'error'."""
        if self.applier is None:
            return "none"
        if not self._apply_lock.acquire(blocking=False):
            return "busy"
        try:
            self.applier()
            return "ran"
        except Exception:
            log.exception("sending approved applications failed")
            return "error"
        finally:
            self._apply_lock.release()

    def reply_tick(self) -> str:
        """Read the answers to the alerts that wait for one. Never overlaps itself, never raises:
        a mail problem must not stop the polling. Returns 'none', 'busy', 'ran' or 'error'."""
        if self.reply_watcher is None:
            return "none"
        if not self._reply_lock.acquire(blocking=False):
            return "busy"
        try:
            self.reply_watcher()
            return "ran"
        except Exception:
            log.exception("reading the replies failed")
            return "error"
        finally:
            self._reply_lock.release()

    def locate_tick(self) -> str:
        """Check the location of the next old ads. Never overlaps itself, never raises: a map
        problem must not stop the polling. Returns 'none', 'busy', 'ran' or 'error'."""
        if self.locator is None:
            return "none"
        if not self._locate_lock.acquire(blocking=False):
            return "busy"
        try:
            self.locator()
            return "ran"
        except Exception:
            log.exception("checking the location of old ads failed")
            return "error"
        finally:
            self._locate_lock.release()

    # ---- one run
    def run_source(self, key: str) -> str:
        """Run one collection pass, then send the alerts it made due. Returns 'ran', 'busy',
        'disabled', 'unknown' or 'error'."""
        outcome = self._collect_once(key)
        if outcome == "ran":
            self.notify_tick()
        return outcome

    def _collect_once(self, key: str) -> str:
        src = self.sources.get(key)
        if src is None:
            return "unknown"
        lock = self._locks[key]
        if not lock.acquire(blocking=False):
            log.info("%s is still running, skipping this tick", key)
            return "busy"
        try:
            with self.session_factory() as session:
                row = get_or_create_source(session, src)  # also fine for "run now" before start()
                if not row.enabled:
                    return "disabled"
                profile = get_or_create_profile(session)
                places = list(session.scalars(select(Place).where(Place.active)))
                pages = src.backlog_pages if row.baselined_at is None else 1
                with self.geocoder_factory(session) as geocoder:
                    collect(session, src, profile, places, geocoder, max_pages=pages)
            return "ran"
        except Exception:
            log.exception("%s: run failed", key)
            return "error"
        finally:
            lock.release()

    run_now = run_source  # the Sources page will call this for a "run now" button
