"""Gmail round 1 (Q3): the alert dispatcher sends what Ivan's rules decide, only to his own address,
only when alerts are on, only for ads that appeared after he switched them on, never twice."""

import smtplib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.profile import get_or_create_profile
from app.models import Application, Contact, Event, Job, Source
from app.notify.dispatch import MAX_URGENT_PER_RUN, dispatch_alerts, make_notifier
from app.notify.gmail import GmailSender
from app.notify.secrets import MemoryStore
from tests.gmail_fakes import ADDRESS, PASSWORD, FakeMail, auth_error

NOW = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)  # 12:00 local
ON_SINCE = NOW - timedelta(hours=1)


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.notify_threshold, p.digest_threshold, p.digest_interval_hours = 60, 40, 3
    p.gmail_address, p.alerts_enabled, p.alerts_enabled_at = ADDRESS, True, ON_SINCE
    p.full_name = "Ivan Primjer"
    db.commit()
    return p


def factory_for(mail):
    return lambda profile: GmailSender(address=profile.gmail_address, password=PASSWORD,
                                       smtp_factory=mail)


def add(db, score, *, title=None, first_seen=None, status="new", channel="email", city="Osijek"):
    source = db.query(Source).filter_by(key="n").first() or Source(key="n", name="n")
    n = db.query(Job).count() + 1
    job = Job(source=source, external_id=f"j{n}", url=f"https://x/{n}", title=title or f"Posao {n}",
              score=score, status=status, channel=channel, city=city,
              first_seen_at=first_seen or NOW - timedelta(minutes=5))
    db.add(job)
    db.commit()
    return job


def run(db, profile, mail, now=NOW):
    return dispatch_alerts(db, profile, sender_factory=factory_for(mail), now=now)


# ---------------------------------------------------------------- when nothing may be sent
def test_nothing_is_sent_while_alerts_are_off(db, profile):
    add(db, 90)
    profile.alerts_enabled = False
    mail = FakeMail()
    result = run(db, profile, mail)
    assert result.stopped == "off" and mail.connects == []


def test_nothing_is_sent_without_a_saved_password_or_address(db, profile):
    add(db, 90)
    mail = FakeMail()
    nobody = dispatch_alerts(db, profile, sender_factory=lambda p: None, now=NOW)
    assert nobody.stopped == "no_gmail"
    profile.gmail_address = None
    assert run(db, profile, mail).stopped == "no_gmail"
    assert mail.connects == []


def test_nothing_is_sent_when_no_ad_deserves_it(db, profile):
    add(db, 30)
    mail = FakeMail()
    result = run(db, profile, mail)
    assert (result.sent_now, result.sent_digest, result.stopped) == (0, 0, None)
    assert mail.connects == []


# ---------------------------------------------------------------- what is sent, and to whom
def test_a_good_ad_alerts_right_away_to_ivan_himself_with_the_full_alert(db, profile):
    job = add(db, 87, title="Konobar vikend", city="Osijek")
    db.add(Contact(job_id=job.id, kind="email", value="posao@luna.example", from_ad=True))
    job.description = "Rad vikendom u kafiću."
    db.commit()
    mail = FakeMail()
    result = run(db, profile, mail)
    assert (result.sent_now, result.stopped) == (1, None)
    msg = mail.sent[0]
    assert msg["To"] == ADDRESS  # never an address from the ad
    assert msg["Subject"].startswith("[87] Konobar vikend")
    assert "posao@luna.example" in msg.get_body(("plain",)).get_content()
    assert "<html" in msg.get_body(("html",)).get_content().lower() or \
        "<div" in msg.get_body(("html",)).get_content()
    db.refresh(job)
    assert job.alerted_at is not None
    assert [e.kind for e in db.scalars(select(Event)) if e.job_id == job.id] == ["alert_now"]


def test_the_waiting_draft_is_part_of_the_alert_but_the_employer_is_never_mailed(db, profile):
    job = add(db, 90, title="Konobar")
    db.add(Application(job_id=job.id, state="queued", to_email="posao@luna.example",
                       subject="Prijava na oglas: Konobar", body="Poštovani, javljam se."))
    db.commit()
    mail = FakeMail()
    run(db, profile, mail)
    text = mail.sent[0].get_body(("plain",)).get_content()
    assert "Poštovani, javljam se." in text
    assert [m["To"] for m in mail.sent] == [ADDRESS]  # the draft is shown, not sent


def test_middle_scores_arrive_together_in_one_digest_and_never_again(db, profile):
    a, b = add(db, 55, title="A"), add(db, 45, title="B")
    mail = FakeMail()
    result = run(db, profile, mail)
    assert (result.sent_now, result.sent_digest) == (0, 2)
    assert len(mail.sent) == 1 and mail.sent[0]["Subject"] == "Skupna obavijest: 2 nova oglasa"
    body = mail.sent[0].get_body(("plain",)).get_content()
    assert "A" in body and "B" in body
    db.refresh(profile)
    assert profile.last_digest_at is not None
    assert all(db.refresh(j) or j.alerted_at is not None for j in (a, b))
    again = run(db, profile, mail, now=NOW + timedelta(hours=4))
    assert (again.sent_now, again.sent_digest) == (0, 0) and len(mail.sent) == 1


def test_an_ad_is_never_alerted_twice(db, profile):
    add(db, 80)
    mail = FakeMail()
    run(db, profile, mail)
    run(db, profile, mail, now=NOW + timedelta(minutes=1))
    assert len(mail.sent) == 1


def test_ads_seen_before_alerts_were_switched_on_are_left_alone(db, profile):
    old = add(db, 95, first_seen=ON_SINCE - timedelta(days=2))
    new = add(db, 70, first_seen=ON_SINCE + timedelta(minutes=10))
    mail = FakeMail()
    assert run(db, profile, mail).sent_now == 1
    db.refresh(old)
    db.refresh(new)
    assert old.alerted_at is None and new.alerted_at is not None


def test_ignored_duplicate_and_unscored_ads_are_not_alerted(db, profile):
    first = add(db, 90)
    dup = add(db, 90)
    dup.duplicate_of = first.id
    add(db, 90, status="ignored")
    add(db, None)
    db.commit()
    mail = FakeMail()
    assert run(db, profile, mail).sent_now == 1


def test_at_most_a_few_urgent_alerts_go_out_per_round_the_rest_wait(db, profile):
    for n in range(MAX_URGENT_PER_RUN + 3):
        add(db, 90 - n)
    mail = FakeMail()
    first = run(db, profile, mail)
    assert first.sent_now == MAX_URGENT_PER_RUN
    second = run(db, profile, mail, now=NOW + timedelta(minutes=1))
    assert second.sent_now == 3
    assert len(mail.sent) == MAX_URGENT_PER_RUN + 3


def test_quiet_hours_hold_urgent_alerts(db, profile):
    profile.quiet_start, profile.quiet_end = "11:00", "13:00"  # local; NOW is 12:00 local
    add(db, 90)
    mail = FakeMail()
    assert run(db, profile, mail).sent_now == 0 and mail.connects == []


# ---------------------------------------------------------------- when Gmail refuses
def test_a_refused_password_switches_alerts_off_logs_it_and_marks_nothing(db, profile):
    job = add(db, 90)
    mail = FakeMail(login_error=auth_error())
    result = run(db, profile, mail)
    assert result.stopped == "auth" and result.sent_now == 0
    db.refresh(profile)
    db.refresh(job)
    assert profile.alerts_enabled is False and "16 slova" in profile.gmail_problem
    assert job.alerted_at is None
    failed = [e for e in db.scalars(select(Event)) if e.kind == "alert_failed"]
    assert len(failed) == 1 and failed[0].data == {"code": "auth", "switched_off": True}
    again = run(db, profile, FakeMail())  # off now: no more attempts
    assert again.stopped == "off"


def test_a_network_failure_keeps_alerts_on_retries_later_and_logs_once_per_half_hour(db, profile):
    job = add(db, 90)
    down = FakeMail(connect_error=OSError("offline"))
    assert run(db, profile, down).stopped == "network"
    assert run(db, profile, down, now=NOW + timedelta(minutes=1)).stopped == "network"
    db.refresh(profile)
    db.refresh(job)
    assert profile.alerts_enabled is True and job.alerted_at is None
    assert len([e for e in db.scalars(select(Event)) if e.kind == "alert_failed"]) == 1
    run(db, profile, down, now=NOW + timedelta(minutes=31))
    assert len([e for e in db.scalars(select(Event)) if e.kind == "alert_failed"]) == 2
    up = FakeMail()
    assert run(db, profile, up, now=NOW + timedelta(minutes=32)).sent_now == 1
    db.refresh(job)
    assert job.alerted_at is not None


def test_a_failure_halfway_keeps_what_was_already_sent_and_retries_only_the_rest(db, profile):
    first, second = add(db, 90, title="Prvi"), add(db, 80, title="Drugi")

    class DiesAfterOne(FakeMail):
        def __call__(self, host, port, timeout=None):
            if len(self.sent) >= 1:
                raise smtplib.SMTPServerDisconnected("gone")
            return super().__call__(host, port, timeout)

    mail = DiesAfterOne()
    result = run(db, profile, mail)
    assert (result.sent_now, result.stopped) == (1, "network")
    db.refresh(first)
    db.refresh(second)
    assert first.alerted_at is not None and second.alerted_at is None
    healthy = FakeMail()
    assert run(db, profile, healthy, now=NOW + timedelta(minutes=1)).sent_now == 1
    assert healthy.sent[0]["Subject"].startswith("[80] Drugi")


def test_the_notifier_for_the_scheduler_uses_its_own_session_and_the_store(db, profile, tmp_path):
    add(db, 90)
    store = MemoryStore()
    store.set(ADDRESS, PASSWORD)
    mail = FakeMail()

    class Factory:
        def __call__(self):
            return _Session()

    class _Session:
        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    import app.notify.dispatch as dispatch_module

    original = dispatch_module.build_sender
    dispatch_module.build_sender = lambda p, s: GmailSender(
        address=p.gmail_address, password=s.get(p.gmail_address), smtp_factory=mail)
    try:
        result = make_notifier(Factory(), store_getter=lambda: store,
                               base_url="http://127.0.0.1:8765")()
    finally:
        dispatch_module.build_sender = original
    assert result.sent_now == 1 and mail.logins == [(ADDRESS, PASSWORD)]
