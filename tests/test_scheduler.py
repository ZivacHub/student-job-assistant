"""Step 3.1: the scheduler polls sources on time, never overlaps, and survives trouble."""

import logging
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.db import make_engine
from app.core.migrate import run_migrations
from app.geo.nominatim import Geocoder, Throttle
from app.main import create_app
from app.models import Source
from app.scheduler import Scheduler
from app.sources.base import BaseSource, ListingItem, RawJob

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class FakeSource(BaseSource):
    name = "Fake"

    def __init__(self, key="fake", interval=10, gate=None, fail=None):
        self.key, self.interval_minutes = key, interval
        self.gate, self.fail = gate, fail
        self.list_calls = 0
        self.entered = threading.Event()
        self.closed = False

    def list_jobs(self, max_pages=1):
        self.list_calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"
        if self.fail:
            raise self.fail
        return []

    def fetch_job(self, item: ListingItem) -> RawJob:  # pragma: no cover
        raise AssertionError("no ads to fetch")

    def close(self):
        self.closed = True


@pytest.fixture
def factory(tmp_path):
    url = f"sqlite:///{(tmp_path / 'sched.db').as_posix()}"
    run_migrations(url)
    engine = make_engine(url)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def offline_geocoder(session):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    return Geocoder(session, http=http, sleep=lambda s: None)


def make(factory, *sources, **kw):
    return Scheduler(factory, {s.key: s for s in sources}, geocoder_factory=offline_geocoder, **kw)


def row(factory, key) -> Source:
    with factory() as s:
        return s.scalar(select(Source).where(Source.key == key))


# ---------- what gets scheduled
def test_one_job_per_enabled_source_with_the_interval_from_the_database(factory):
    sched = make(factory, FakeSource("a", interval=7), FakeSource("b", interval=15))
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a", "b"]
        job = sched._scheduler.get_job("a")
        assert job.trigger.interval == timedelta(minutes=7)
    finally:
        sched.shutdown()


def test_disabled_sources_and_interval_zero_are_not_scheduled(factory):
    off, paste = FakeSource("off"), FakeSource("paste", interval=0)
    with factory() as s:
        from app.sources.ingest import get_or_create_source

        get_or_create_source(s, off).enabled = False
        s.commit()
    sched = make(factory, off, paste, FakeSource("on"))
    sched.start()
    try:
        assert sched.scheduled_keys() == ["on"]
    finally:
        sched.shutdown()


# ---------- timing after a restart
def test_first_run_is_soon_when_never_run(factory):
    sched = make(factory, FakeSource(), startup_delay_s=5)
    r = Source(key="x", name="x", interval_minutes=10, last_run_at=None)
    assert sched.first_run_time(r, NOW) == NOW + timedelta(seconds=5)


def test_restart_does_not_poll_again_before_the_interval_has_passed(factory):
    sched = make(factory, FakeSource(), startup_delay_s=5)
    recent = Source(key="x", name="x", interval_minutes=10, last_run_at=NOW - timedelta(minutes=2))
    assert sched.first_run_time(recent, NOW) == NOW + timedelta(minutes=8)


def test_restart_after_a_long_pause_polls_soon_not_in_the_past(factory):
    sched = make(factory, FakeSource(), startup_delay_s=5)
    old = Source(key="x", name="x", interval_minutes=10, last_run_at=NOW - timedelta(hours=3))
    assert sched.first_run_time(old, NOW) == NOW + timedelta(seconds=5)


def test_naive_last_run_from_sqlite_is_read_as_utc(factory):
    sched = make(factory, FakeSource(), startup_delay_s=5)
    naive = Source(key="x", name="x", interval_minutes=10,
                   last_run_at=(NOW - timedelta(minutes=2)).replace(tzinfo=None))
    assert sched.first_run_time(naive, NOW) == NOW + timedelta(minutes=8)


# ---------- one run
def test_a_run_goes_through_collect_and_is_recorded(factory):
    src = FakeSource()
    sched = make(factory, src)
    assert sched.run_source("fake") == "ran"
    r = row(factory, "fake")
    assert src.list_calls == 1 and r.last_status == "ok" and r.last_run_at is not None


def test_first_run_reads_the_backlog_pages_later_runs_only_the_newest(factory):
    seen = []

    class Spy(FakeSource):
        def list_jobs(self, max_pages=1):
            seen.append(max_pages)
            return []

    sched = make(factory, Spy())
    sched.run_source("fake")  # nothing stored yet -> not baselined -> backlog
    sched.run_source("fake")  # clean first run completed the baseline -> page 1 only
    assert seen == [3, 1]


def test_a_source_is_never_run_twice_at_the_same_time(factory):
    gate = threading.Event()
    src = FakeSource(gate=gate)
    sched = make(factory, src)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("first", sched.run_source("fake")))
    t.start()
    assert src.entered.wait(10)  # the first run is now inside list_jobs
    assert sched.run_source("fake") == "busy"  # the second returns at once, does not wait
    gate.set()
    t.join(10)
    assert result["first"] == "ran" and src.list_calls == 1


def test_switching_a_source_off_takes_effect_on_the_next_run(factory):
    src = FakeSource()
    sched = make(factory, src)
    sched.run_source("fake")
    with factory() as s:
        s.scalar(select(Source).where(Source.key == "fake")).enabled = False
        s.commit()
    assert sched.run_source("fake") == "disabled"
    assert src.list_calls == 1


def test_unknown_source_key(factory):
    assert make(factory, FakeSource()).run_source("nope") == "unknown"


def test_a_crash_inside_a_source_is_recorded_and_the_lock_is_released(factory):
    src = FakeSource(fail=RuntimeError("parser bug"))
    sched = make(factory, src)
    assert sched.run_source("fake") == "ran"  # collect() records the problem itself
    r = row(factory, "fake")
    assert r.last_status == "error" and "RuntimeError: parser bug" in r.last_error
    assert sched.run_source("fake") == "ran"  # not stuck as 'busy'


def test_one_failing_source_does_not_stop_the_other(factory):
    bad, good = FakeSource("bad", fail=RuntimeError("x")), FakeSource("good")
    sched = make(factory, bad, good)
    sched.run_source("bad")
    assert sched.run_source("good") == "ran" and good.list_calls == 1


# ---------- shutdown and the app
def test_shutdown_stops_polling_and_closes_the_sources(factory):
    src = FakeSource()
    sched = make(factory, src)
    sched.start()
    assert sched.running
    sched.shutdown()
    assert not sched.running and src.closed


class Stub:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def shutdown(self, wait=False):
        self.events.append("shutdown")


def test_app_starts_and_stops_its_scheduler():
    stub = Stub()
    with TestClient(create_app(scheduler=stub)) as client:
        assert client.get("/health").status_code == 200
        assert stub.events == ["start"]
    assert stub.events == ["start", "shutdown"]


def test_app_without_scheduler_when_disabled_by_settings():
    # tests/conftest.py sets JOBS_SCHEDULER_ENABLED=false
    app = create_app()
    with TestClient(app):
        assert app.state.scheduler is None


def test_running_migrations_does_not_wipe_the_apps_logging(tmp_path):
    root = logging.getLogger()
    marker = logging.NullHandler()
    root.addHandler(marker)
    try:
        run_migrations(f"sqlite:///{(tmp_path / 'log.db').as_posix()}")
        assert marker in root.handlers
    finally:
        root.removeHandler(marker)


# ---------- the shared geocoder limit
def test_two_geocoders_sharing_a_throttle_respect_one_request_per_second():
    t = [100.0]
    sleeps = []
    shared = Throttle(1.1, sleeps.append, lambda: t[0])
    shared.wait()  # first geocoder's request
    t[0] += 0.3
    shared.wait()  # second geocoder, 0.3 s later
    assert sleeps == [pytest.approx(0.8)]


# ---------- what the Sources page needs (step 4.4)
def change_row(factory, key, **values):
    with factory() as s:
        r = s.scalar(select(Source).where(Source.key == key))
        for name, value in values.items():
            setattr(r, name, value)
        s.commit()


def test_next_run_is_known_only_while_the_scheduler_runs(factory):
    sched = make(factory, FakeSource("a"))
    assert sched.next_run("a") is None  # not started
    sched.start()
    try:
        assert sched.next_run("a") is not None
        assert sched.next_run("unknown") is None
    finally:
        sched.shutdown()


def test_is_running_is_true_during_a_run_only(factory):
    gate = threading.Event()
    src = FakeSource("a", gate=gate)
    sched = make(factory, src)
    assert sched.is_running("a") is False and sched.is_running("nope") is False
    worker = threading.Thread(target=sched.run_source, args=("a",))
    worker.start()
    try:
        assert src.entered.wait(10)
        assert sched.is_running("a") is True
    finally:
        gate.set()
        worker.join(10)
    assert sched.is_running("a") is False


def test_switching_a_source_off_and_on_changes_the_schedule(factory):
    sched = make(factory, FakeSource("a", interval=10))
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a"]
        change_row(factory, "a", enabled=False)
        sched.sync_source("a")
        assert sched.scheduled_keys() == []
        sched.sync_source("a")  # nothing to remove: no error
        change_row(factory, "a", enabled=True)
        sched.sync_source("a")
        assert sched.scheduled_keys() == ["a"]
    finally:
        sched.shutdown()


def test_a_new_interval_is_applied_to_the_running_schedule(factory):
    sched = make(factory, FakeSource("a", interval=10))
    sched.start()
    try:
        change_row(factory, "a", interval_minutes=30)
        sched.sync_source("a")
        assert sched._scheduler.get_job("a").trigger.interval == timedelta(minutes=30)
        change_row(factory, "a", interval_minutes=0)  # 0 = never polled
        sched.sync_source("a")
        assert sched.scheduled_keys() == []
    finally:
        sched.shutdown()


def test_sync_ignores_unknown_sources_and_a_stopped_scheduler(factory):
    sched = make(factory, FakeSource("a"))
    sched.sync_source("a")  # not started: nothing happens, no error
    sched.sync_source("unknown")
    assert sched.scheduled_keys() == []


def test_knows_which_sources_have_a_checker(factory):
    sched = make(factory, FakeSource("a"))
    assert sched.knows("a") is True and sched.knows("paste") is False
