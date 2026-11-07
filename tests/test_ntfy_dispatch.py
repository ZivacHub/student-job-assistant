"""ntfy phone push (N2): the dispatcher sends one push per immediate alert and one per digest, only
after the email went out, with no personal data, and a failed push never touches the email."""

import json
import smtplib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.profile import get_or_create_profile
from app.models import Contact, Event
from app.notify.dispatch import dispatch_alerts
from app.notify.ntfy import NtfyError, NtfySender
from tests.gmail_fakes import ADDRESS, FakeMail
from tests.test_gmail_dispatch import NOW, ON_SINCE, add, factory_for

TOPIC = "sja-abcd-efgh-ijkl-mnop"


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.notify_threshold, p.digest_threshold, p.digest_interval_hours = 60, 40, 3
    p.gmail_address, p.alerts_enabled, p.alerts_enabled_at = ADDRESS, True, ON_SINCE
    p.full_name = "Ivan Primjer"
    db.commit()
    return p


class Pusher:
    """A pretend phone push that records what it was asked to send (and in which order)."""

    def __init__(self, *, fail=None, log=None):
        self.fail = fail
        self.calls = []
        self.log = log if log is not None else []

    def publish(self, *, title, message, priority, click):
        self.log.append("push")
        if self.fail:
            raise self.fail
        self.calls.append({"title": title, "message": message, "priority": priority,
                           "click": click})


def run(db, profile, mail, pusher, now=NOW):
    return dispatch_alerts(db, profile, sender_factory=factory_for(mail),
                           push_factory=(lambda p: pusher) if pusher is not None else None,
                           now=now)


def on(profile, db):
    profile.ntfy_enabled = True
    db.commit()


# ---------------------------------------------------------------- what is sent
def test_an_immediate_alert_also_sends_one_push_after_the_email(db, profile):
    on(profile, db)
    job = add(db, 87, title="Konobar vikend", city="Osijek")
    job.url = "https://www.studentski-poslovi.hr/oglas/77"
    db.commit()
    order = []

    class OrderedMail(FakeMail):
        def __call__(self, host, port, timeout=None):
            conn = super().__call__(host, port, timeout)
            real = conn.send_message
            conn.send_message = lambda msg: (order.append("email"), real(msg))[1]
            return conn

    mail, pusher = OrderedMail(), Pusher(log=order)
    run(db, profile, mail, pusher)
    assert order == ["email", "push"]  # the email first
    (push,) = pusher.calls
    assert push["title"] == "Novi posao" and push["priority"] == 4
    assert push["message"] == mail.sent[0]["Subject"] and push["message"].startswith("[87] Konobar")
    assert push["click"] == "https://www.studentski-poslovi.hr/oglas/77"


def test_a_digest_sends_one_normal_priority_push_without_a_link(db, profile):
    on(profile, db)
    add(db, 55, title="A")
    add(db, 45, title="B")
    pusher = Pusher()
    run(db, profile, FakeMail(), pusher)
    (push,) = pusher.calls
    assert push["title"] == "Skupna obavijest" and push["priority"] == 3 and push["click"] is None
    assert "2 nova oglasa" in push["message"]


def test_no_push_when_the_phone_switch_is_off_or_there_is_nothing_to_push_with(db, profile):
    add(db, 90)
    pusher = Pusher()
    run(db, profile, FakeMail(), pusher)  # switch off
    assert pusher.calls == []
    on(profile, db)
    add(db, 91)
    run(db, profile, FakeMail(), None, now=NOW + timedelta(minutes=1))  # no factory at all
    nobody = dispatch_alerts(db, profile, sender_factory=factory_for(FakeMail()),
                             push_factory=lambda p: None, now=NOW + timedelta(minutes=2))
    assert nobody.stopped is None  # no topic stored: no push, no trouble


def test_the_push_holds_no_personal_data_from_the_ad(db, profile):
    on(profile, db)
    job = add(db, 90, title="Konobar vikend", city="Osijek")
    job.description = "Javite se Marku Horvatu, tel 031 555 777, marko@firma.example"
    db.add(Contact(job_id=job.id, kind="phone", value="+38531555777", from_ad=True))
    db.add(Contact(job_id=job.id, kind="email", value="marko@firma.example", from_ad=True))
    db.commit()
    pusher = Pusher()
    run(db, profile, FakeMail(), pusher)
    sent = json.dumps(pusher.calls)
    for secret in ("555", "marko@", "Horvat", "Ivan Primjer", "ivan.test@gmail.com", "Javite se"):
        assert secret not in sent


def test_an_unsafe_ad_address_is_not_passed_on_as_a_click_target(db, profile):
    on(profile, db)
    job = add(db, 90)
    job.url = "javascript:alert(1)"
    db.commit()
    pusher = Pusher()
    run(db, profile, FakeMail(), pusher)
    assert pusher.calls[0]["click"] is None


def test_the_real_sender_posts_json_to_ntfy_end_to_end(db, profile):
    on(profile, db)
    add(db, 80, title="Skladištar")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch_alerts(db, profile, sender_factory=factory_for(FakeMail()),
                    push_factory=lambda p: NtfySender(TOPIC, client=client), now=NOW)
    assert len(seen) == 1 and seen[0]["topic"] == TOPIC and seen[0]["priority"] == 4
    assert seen[0]["message"].startswith("[80] Skladištar")


# ---------------------------------------------------------------- when the push fails
def test_a_failed_push_never_blocks_repeats_or_unmarks_the_email(db, profile):
    on(profile, db)
    first, second = add(db, 90, title="Prvi"), add(db, 80, title="Drugi")
    mail = FakeMail()
    pusher = Pusher(fail=NtfyError("network", "x"))
    result = run(db, profile, mail, pusher)
    assert (result.sent_now, result.stopped) == (2, None)
    assert len(mail.sent) == 2  # both emails went out
    db.refresh(first)
    db.refresh(second)
    assert first.alerted_at is not None and second.alerted_at is not None
    again = run(db, profile, mail, pusher, now=NOW + timedelta(minutes=1))
    assert again.sent_now == 0 and len(mail.sent) == 2  # nothing is sent twice


def test_a_failed_push_is_logged_at_most_once_every_half_hour(db, profile):
    on(profile, db)
    pusher = Pusher(fail=NtfyError("refused", "x"))
    add(db, 90)
    run(db, profile, FakeMail(), pusher)
    add(db, 85)
    run(db, profile, FakeMail(), pusher, now=NOW + timedelta(minutes=5))
    failed = [e for e in db.scalars(select(Event)) if e.kind == "push_failed"]
    assert len(failed) == 1 and failed[0].data == {"code": "refused"}
    add(db, 84)
    run(db, profile, FakeMail(), pusher, now=NOW + timedelta(minutes=40))
    assert len([e for e in db.scalars(select(Event)) if e.kind == "push_failed"]) == 2


def test_no_push_when_the_email_could_not_be_sent(db, profile):
    on(profile, db)
    add(db, 90)
    pusher = Pusher()
    down = FakeMail(connect_error=smtplib.SMTPServerDisconnected("gone"))
    result = run(db, profile, down, pusher)
    assert result.stopped == "network" and pusher.calls == []
