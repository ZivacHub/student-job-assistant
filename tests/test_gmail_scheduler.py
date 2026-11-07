"""Gmail round 1 (Q5): the scheduler sends the due alerts after every source run and once a minute,
never overlaps itself, and a failure in it never stops the polling of the sources."""

import threading
from datetime import timedelta

from tests.test_scheduler import FakeSource, factory, make  # noqa: F401  (factory is a fixture)


class Recorder:
    def __init__(self, *, fail=None, gate=None):
        self.calls = 0
        self.fail = fail
        self.gate = gate
        self.entered = threading.Event()

    def __call__(self):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"
        if self.fail:
            raise self.fail


def test_alerts_are_sent_after_every_successful_source_run(factory):  # noqa: F811
    notifier = Recorder()
    sched = make(factory, FakeSource("a"), notifier=notifier)
    assert sched.run_source("a") == "ran"
    assert sched.run_source("a") == "ran"
    assert notifier.calls == 2


def test_no_alert_round_after_a_run_that_did_nothing(factory):  # noqa: F811
    notifier = Recorder()
    sched = make(factory, FakeSource("a"), notifier=notifier)
    assert sched.run_source("nope") == "unknown"
    with factory() as s:
        from app.sources.ingest import get_or_create_source

        get_or_create_source(s, sched.sources["a"]).enabled = False
        s.commit()
    assert sched.run_source("a") == "disabled"
    assert notifier.calls == 0


def test_a_failing_alert_round_never_breaks_polling(factory):  # noqa: F811
    notifier = Recorder(fail=RuntimeError("smtp exploded"))
    sched = make(factory, FakeSource("a"), notifier=notifier)
    assert sched.run_source("a") == "ran"  # the source run itself still counts as fine
    assert sched.notify_tick() == "error"
    assert notifier.calls == 2


def test_a_failing_site_does_not_stop_the_alert_rounds(factory):  # noqa: F811
    notifier = Recorder()
    sched = make(factory, FakeSource("a", fail=RuntimeError("site down")), notifier=notifier)
    # a failing site is recorded by the collector, so the run still counts as done and the alert
    # round after it is harmless (it only sends what is already due)
    assert sched.run_source("a") == "ran"
    assert sched.notify_tick() == "ran"


def test_two_alert_rounds_never_overlap(factory):  # noqa: F811
    gate = threading.Event()
    notifier = Recorder(gate=gate)
    sched = make(factory, FakeSource("a"), notifier=notifier)
    worker = threading.Thread(target=sched.notify_tick)
    worker.start()
    assert notifier.entered.wait(10)
    assert sched.notify_tick() == "busy"  # a second one while the first is still running
    gate.set()
    worker.join(10)
    assert notifier.calls == 1
    assert sched.notify_tick() == "ran"


def test_without_a_notifier_nothing_happens_and_no_alert_job_is_scheduled(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"))
    assert sched.notify_tick() == "none"
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a"]
    finally:
        sched.shutdown()


def test_with_a_notifier_an_alert_job_runs_every_minute_next_to_the_sources(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"), notifier=Recorder())
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a", "notify"]
        assert sched._scheduler.get_job("notify").trigger.interval == timedelta(minutes=1)
    finally:
        sched.shutdown()


def test_the_real_app_builds_its_scheduler_with_the_alert_notifier():
    import inspect

    from app import main

    assert "notifier=make_notifier(" in inspect.getsource(main.create_app)
