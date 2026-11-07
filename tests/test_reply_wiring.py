"""Plan 5.3b (D42): the parts around the reply flow: which alerts get a code, the scheduler job,
the log wording, the new tables, and the reader's own limits seen from the app."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import select, text

from app.apply import replycode
from app.apply.replyflow import make_reply_watcher
from app.core.db import make_engine
from app.core.event_log import REPLY_IGNORED_WHY, _describe
from app.core.profile import get_or_create_profile
from app.models import Application, ApprovalCode, Contact, Event, Job, Source
from app.notify.alerts import build_alert
from app.notify.dispatch import dispatch_alerts
from app.notify.gmail import GmailSender
from app.notify.secrets import MemoryStore
from tests.gmail_fakes import ADDRESS, PASSWORD, FakeMail
from tests.test_migrations_keep_data import cfg_for
from tests.test_scheduler import FakeSource, factory, make  # noqa: F401  (factory is a fixture)

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.notify_threshold, p.digest_threshold, p.digest_interval_hours = 60, 40, 3
    p.gmail_address, p.alerts_enabled = ADDRESS, True
    p.alerts_enabled_at = NOW - timedelta(hours=1)
    db.commit()
    return p


def factory_for(mail):
    return lambda p: GmailSender(address=p.gmail_address, password=PASSWORD, smtp_factory=mail)


def add_job(db, n=1, *, channel="email", score=90):
    source = db.scalar(select(Source).where(Source.key == "n")) or Source(key="n", name="n")
    job = Job(source=source, external_id=f"j{n}", url=f"https://x/{n}", title=f"Posao {n}",
              score=score, status="new", channel=channel, city="Osijek",
              first_seen_at=NOW - timedelta(minutes=5))
    db.add(job)
    db.commit()
    return job


def draft(db, job, state="queued"):
    application = Application(job_id=job.id, state=state, to_email="posao@luna.example",
                              subject="Prijava", body="Poštovani, javljam se.")
    db.add(application)
    db.commit()
    return application


# ---------------------------------------------------------------- which alerts get a code
def test_an_alert_with_a_waiting_draft_gets_a_code_that_is_live_once_gmail_took_it(db, profile):
    job = add_job(db)
    application = draft(db, job)
    mail = FakeMail()
    dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=NOW)
    (message,) = mail.sent
    (code,) = replycode.find_codes(message["Subject"])
    row = replycode.find_code(db, code, NOW)
    assert row.application_id == application.id and row.sent_at is not None
    assert row.alert_message_id == message["Message-ID"]  # tied to the mail that was really sent
    assert row.expires_at.replace(tzinfo=UTC) - NOW == replycode.CODE_TTL


@pytest.mark.parametrize("state", ["approved", "skipped"])
def test_a_draft_that_no_longer_waits_gets_no_code(db, profile, state):
    draft(db, add_job(db), state)
    mail = FakeMail()
    dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=NOW)
    assert "SJA-" not in mail.sent[0]["Subject"] and db.scalars(select(ApprovalCode)).all() == []


def test_alerts_without_a_draft_get_no_code(db, profile):
    add_job(db, 1, channel="phone")
    add_job(db, 2, channel="email")  # an email ad that has no draft
    mail = FakeMail()
    dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=NOW)
    assert len(mail.sent) == 2
    assert all("SJA-" not in m["Subject"] and "DA" not in m.get_body(("plain",)).get_content()
               .split("KAKO SE JAVITI")[0] for m in mail.sent)
    assert db.scalars(select(ApprovalCode)).all() == []


def test_an_alert_that_gmail_refused_leaves_no_live_code(db, profile):
    draft(db, add_job(db))
    mail = FakeMail(send_error=OSError("no network"))
    dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=NOW)
    assert db.scalars(select(ApprovalCode)).all() == []
    assert replycode.live_codes(db, NOW) == []


def test_a_digest_carries_no_code(db, profile):
    add_job(db, 1, score=50)
    add_job(db, 2, score=45)
    mail = FakeMail()
    dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=NOW)
    assert "SJA-" not in mail.sent[0]["Subject"]
    assert db.scalars(select(ApprovalCode)).all() == []


def test_each_alert_gets_its_own_code(db, profile):
    draft(db, add_job(db, 1))
    draft(db, add_job(db, 2))
    mail = FakeMail()
    dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=NOW)
    codes = [replycode.find_codes(m["Subject"])[0] for m in mail.sent]
    assert len(set(codes)) == 2


def test_the_text_explains_how_to_answer_and_the_title_keeps_its_old_start(db, profile):
    job = add_job(db)
    mail = FakeMail()
    draft(db, job)
    dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=NOW)
    subject = mail.sent[0]["Subject"]
    assert subject.startswith("[90] Posao 1") and subject.endswith("]")
    text_part = mail.sent[0].get_body(("plain",)).get_content()
    assert "ODOBRENJE S MOBITELA" in text_part and "Odgovorite NE" in text_part


def test_build_alert_without_a_draft_ignores_a_marker():
    job = Job(source_id=1, external_id="1", url="https://x", title="T", status="new")
    job.contacts = [Contact(kind="email", value="a@b.example", from_ad=True, confidence=0.9)]
    alert = build_alert(job, approval_marker="SJA-AAAA-BBBB")
    assert "SJA-" not in alert.subject and "SJA-" not in alert.text


# ---------------------------------------------------------------- the scheduler job
class Recorder:
    def __init__(self, *, fail=None, gate=None):
        self.calls, self.fail, self.gate = 0, fail, gate
        self.entered = threading.Event()

    def __call__(self):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"
        if self.fail:
            raise self.fail


def test_without_a_reply_watcher_nothing_is_scheduled(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"))
    assert sched.reply_tick() == "none"
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a"]
    finally:
        sched.shutdown()


def test_with_a_reply_watcher_a_job_runs_every_minute(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"), reply_watcher=Recorder())
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a", "replies"]
        assert sched._scheduler.get_job("replies").trigger.interval == timedelta(minutes=1)
    finally:
        sched.shutdown()


def test_a_failing_reply_watcher_never_raises_and_never_overlaps(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"), reply_watcher=Recorder(fail=RuntimeError("boom")))
    assert sched.reply_tick() == "error"
    gate = threading.Event()
    slow = Recorder(gate=gate)
    sched = make(factory, FakeSource("a"), reply_watcher=slow)
    worker = threading.Thread(target=sched.reply_tick)
    worker.start()
    assert slow.entered.wait(10)
    assert sched.reply_tick() == "busy"
    gate.set()
    worker.join(10)
    assert sched.reply_tick() == "ran" and slow.calls == 2


def test_the_real_app_builds_its_scheduler_with_the_reply_watcher():
    import inspect

    from app import main

    assert "reply_watcher=make_reply_watcher(" in inspect.getsource(main.create_app)


def test_the_watcher_the_scheduler_calls_does_nothing_while_no_alert_waits(factory):  # noqa: F811
    result = make_reply_watcher(factory, store_getter=MemoryStore)()
    assert (result.examined, result.stopped) == (0, None)


def test_the_watcher_without_a_saved_password_stops_quietly_when_an_alert_waits(factory):  # noqa: F811
    with factory() as s:
        profile = get_or_create_profile(s)
        profile.gmail_address = ADDRESS
        job = add_job(s)
        application = draft(s, job)
        # the watcher reads the real clock, so the code must be live NOW: with the fixed date
        # NOW it expired 24 hours later and this test went red (found 2026-11-07)
        moment = datetime.now(UTC)
        replycode.issue_code(s, application, alert_message_id="<a@gmail.com>", now=moment)
        replycode.mark_sent(s, application.id, "<a@gmail.com>", moment)
        s.commit()
    assert make_reply_watcher(factory, store_getter=MemoryStore)().stopped == "no_gmail"


# ---------------------------------------------------------------- the log in Croatian
def describe(kind, data, job_id=None):
    event = Event(kind=kind, data=data, job_id=job_id)
    return _describe(event, {}, {})


def test_every_reason_has_a_plain_croatian_line():
    for reason, why in REPLY_IGNORED_WHY.items():
        text_, level = describe("reply_ignored", {"result": reason})
        assert why in text_ and level == "warn"
    assert "nije u redu" in describe("reply_ignored", {"result": "something new"})[0]
    shown = describe("reply_ignored", {"result": "not_authenticated",
                                       "verdict": "mx.google.com: dkim=fail"})[0]
    assert "Gmail javlja: mx.google.com: dkim=fail" in shown


def test_the_other_new_kinds_read_plainly():
    assert describe("reply_approved", {"result": "approved"})[1] == "ok"
    assert "preskočili" in describe("reply_cancelled", {"result": "skipped"})[0]
    assert "poništili" in describe("reply_cancelled", {"result": "cancelled"})[0]
    assert "IMAP" in describe("reply_watch_failed", {"code": "auth"})[0]
    assert describe("reply_watch_failed", {"code": "auth"})[1] == "bad"
    assert "nema veze" in describe("reply_watch_failed", {"code": "network"})[0]
    assert "ipak vrijedi" in describe("reply_mail_failed", {"code": "network"})[0]


# ---------------------------------------------------------------- the new tables, old data kept
def test_the_new_tables_arrive_and_old_rows_survive(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, "a8c3d5e7f912")  # the version before step 5.3b
    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO source (id, key, name, enabled, interval_minutes,"
                       " consecutive_failures, last_items) VALUES (1, 's', 's', 1, 10, 0, 0)"))
        c.execute(text("INSERT INTO job (id, source_id, external_id, url, title, status, channel,"
                       " first_seen_at) VALUES (1, 1, 'e1', 'https://x', 'Konobar', 'new',"
                       " 'email', '2026-10-09 00:00:00')"))
        c.execute(text("INSERT INTO application (id, job_id, state, dry_run, created_at) "
                       "VALUES (1, 1, 'queued', 1, '2026-10-09 00:00:00')"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        counts = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                  for t in ("source", "job", "application", "approval_code", "approval_reply")}
        errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
    engine.dispose()
    assert counts == {"source": 1, "job": 1, "application": 1, "approval_code": 0,
                      "approval_reply": 0}
    assert errors == []


def test_a_message_is_recorded_only_once(db, profile):
    from sqlalchemy.exc import IntegrityError

    from app.models import ApprovalReply

    db.add(ApprovalReply(message_id="<x@y>", outcome="approved"))
    db.commit()
    db.add(ApprovalReply(message_id="<x@y>", outcome="again"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
