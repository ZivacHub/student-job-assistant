"""Real sending (S4): the scheduler sends the due approvals every minute through its own session,
never overlaps itself, and a failure there never stops source polling or alerts. The page tick and
the scheduler together send each application once. Nothing real is contacted."""

import threading
from datetime import timedelta

import pytest

from app.apply.runner import make_applier
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.models import Cv, Source
from app.models.base import utcnow
from app.notify.secrets import get_secret_store
from tests.gmail_fakes import ADDRESS, PASSWORD, FakeMail
from tests.test_apply_service import add_job
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


def test_without_an_applier_nothing_happens_and_no_job_is_scheduled(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"))
    assert sched.apply_tick() == "none"
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a"]
    finally:
        sched.shutdown()


def test_with_an_applier_a_job_runs_every_minute_next_to_the_sources(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"), notifier=Recorder(), applier=Recorder())
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a", "apply", "notify"]
        assert sched._scheduler.get_job("apply").trigger.interval == timedelta(minutes=1)
    finally:
        sched.shutdown()


def test_a_tick_calls_the_applier_and_a_failure_never_raises(factory):  # noqa: F811
    applier = Recorder(fail=RuntimeError("smtp exploded"))
    sched = make(factory, FakeSource("a"), applier=applier)
    assert sched.apply_tick() == "error"
    assert sched.run_source("a") == "ran"  # polling is not affected
    assert applier.calls == 1


def test_two_ticks_never_overlap(factory):  # noqa: F811
    gate = threading.Event()
    applier = Recorder(gate=gate)
    sched = make(factory, FakeSource("a"), applier=applier)
    worker = threading.Thread(target=sched.apply_tick)
    worker.start()
    assert applier.entered.wait(10)
    assert sched.apply_tick() == "busy"
    gate.set()
    worker.join(10)
    assert applier.calls == 1 and sched.apply_tick() == "ran"


def test_a_failing_applier_does_not_stop_the_alerts(factory):  # noqa: F811
    notifier = Recorder()
    sched = make(factory, FakeSource("a"), notifier=notifier,
                 applier=Recorder(fail=RuntimeError("boom")))
    assert sched.apply_tick() == "error"
    assert sched.notify_tick() == "ran" and notifier.calls == 1


def test_the_real_app_builds_its_scheduler_with_the_applier():
    import inspect

    from app import main

    assert "applier=make_applier(" in inspect.getsource(main.create_app)


# ---------------------------------------------------------------- the real applier, end to end
class _Sessions:
    """Gives the applier the test's own session (a context manager like a session factory)."""

    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        return False


@pytest.fixture
def live(db, tmp_path, monkeypatch):
    """Real sending on (the pretend way), a Gmail password in the memory store, a CV file."""
    monkeypatch.setattr(get_settings(), "dry_run", False)
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF-1.4 fake")
    profile = get_or_create_profile(db)
    profile.full_name, profile.notify_threshold = "Ivan Primjer", 60
    profile.gmail_address, profile.gmail_checked_at = ADDRESS, utcnow()
    profile.cvs.append(Cv(label="Hrvatski", language="hr", path="cvs/hr.pdf", is_default=True))
    get_secret_store().set(ADDRESS, PASSWORD)
    db.commit()
    mail = FakeMail()
    monkeypatch.setattr("app.apply.runner.smtplib.SMTP", mail)
    return profile, mail


def due_application(db, profile, n=1):
    from app.apply import service

    source = db.query(Source).first() or Source(key="sp", name="studentski-poslovi.hr")
    db.add(source)
    db.flush()
    now = utcnow()
    job = add_job(db, source, n, email=f"posao{n}@example.hr")
    job.first_seen_at = now - timedelta(hours=1)
    application = service.ensure_draft(db, job, profile, now)
    assert service.approve(db, application, profile, now - timedelta(minutes=5),
                           dry_run=False) == {}
    application.send_after = now - timedelta(minutes=1)
    db.commit()
    return application


def test_the_applier_sends_a_due_approval_with_its_own_session_and_the_saved_password(db, live):
    profile, mail = live
    application = due_application(db, profile)
    applier = make_applier(_Sessions(db), store_getter=get_secret_store,
                           cv_dir_getter=lambda: get_settings().data_dir / "cvs")
    outcomes = applier()
    assert [o.kind for o in outcomes] == ["sent"]
    assert [m["To"] for m in mail.sent] == ["posao1@example.hr"]
    assert mail.logins == [(ADDRESS, PASSWORD)] and application.state == "sent"
    assert applier() == [] and len(mail.sent) == 1  # the next minute sends nothing more


def test_in_trial_mode_the_applier_only_writes_the_log(db, live, monkeypatch):
    profile, mail = live
    monkeypatch.setattr(get_settings(), "dry_run", True)
    application = due_application(db, profile)
    application.dry_run = True
    db.commit()
    applier = make_applier(_Sessions(db), store_getter=get_secret_store,
                           cv_dir_getter=lambda: get_settings().data_dir / "cvs")
    assert [o.kind for o in applier()] == ["dry_run"] and mail.connects == []


def test_without_a_saved_password_the_applier_sends_nothing_and_does_not_fail(db, live):
    profile, mail = live
    due_application(db, profile)
    get_secret_store().delete(ADDRESS)
    applier = make_applier(_Sessions(db), store_getter=get_secret_store,
                           cv_dir_getter=lambda: get_settings().data_dir / "cvs")
    assert [o.kind for o in applier()] == ["blocked"] and mail.connects == []
