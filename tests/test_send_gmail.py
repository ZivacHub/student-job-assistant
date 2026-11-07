"""Real sending (S2): the real Gmail sender used through the approval gate delivers one message to
the address of the ad with the approved text and the CV attached, and nothing in every case the
gate must refuse. A pretend mail server; nothing real is contacted."""

from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.apply import gate, service
from app.apply.gate import process_due
from app.apply.runner import sender_for
from app.core.profile import get_or_create_profile
from app.models import Contact, Cv, Source
from app.notify.secrets import MemoryStore
from tests.gmail_fakes import ADDRESS, PASSWORD, FakeMail, auth_error
from tests.test_apply_gate import LATER, NOW, approved
from tests.test_apply_service import add_job


@pytest.fixture
def src(db):
    s = Source(key="sp", name="studentski-poslovi.hr")
    db.add(s)
    db.flush()
    return s


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.full_name, p.availability, p.notify_threshold = "Ivan Primjer", "vikendom", 60
    p.gmail_address = ADDRESS
    p.cvs.append(Cv(label="Hrvatski", language="hr", path="cvs/hr.pdf", is_default=True))
    db.flush()
    return p


@pytest.fixture
def store():
    s = MemoryStore()
    s.set(ADDRESS, PASSWORD)
    return s


@pytest.fixture
def cv_dir(tmp_path):
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF-1.4 fake cv")
    return tmp_path / "cvs"


def run(db, profile, store, mail, cv_dir, *, dry_run=False, now=LATER):
    sender = sender_for(profile, store, smtp_factory=mail)
    return process_due(db, profile, sender, dry_run=dry_run, cv_dir=cv_dir, now=now)


# ---------------------------------------------------------------- the positive control
def test_an_approved_draft_is_delivered_once_with_the_exact_text_and_the_cv(
        db, src, profile, store, cv_dir):
    application = approved(db, src, profile)
    text = (application.to_email, application.subject, application.body)
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, cv_dir)
    assert [o.kind for o in outcomes] == ["sent"]
    [msg] = mail.sent
    assert (msg["To"], msg["Subject"]) == (text[0], text[1])
    assert msg.get_body(("plain",)).get_content().strip() == text[2].strip()
    [cv] = list(msg.iter_attachments())
    assert cv.get_filename() == "hr.pdf" and cv.get_content_type() == "application/pdf"
    assert cv.get_content() == b"%PDF-1.4 fake cv"
    assert ADDRESS in msg["From"] and "Ivan Primjer" in msg["From"]
    assert mail.logins == [(ADDRESS, PASSWORD)] and mail.tls == 1 and mail.quits == 1
    assert application.state == "sent" and application.message_id == msg["Message-ID"]
    # three more rounds, still one message
    for minutes in (1, 2, 3):
        run(db, profile, store, mail, cv_dir, now=LATER + timedelta(minutes=minutes))
    assert len(mail.sent) == 1


def test_the_recipient_is_always_an_address_printed_in_the_ad(db, src, profile, store, cv_dir):
    application = approved(db, src, profile, email="posao@firma.example")
    mail = FakeMail()
    run(db, profile, store, mail, cv_dir)
    assert [m["To"] for m in mail.sent] == ["posao@firma.example"]
    assert application.to_email == "posao@firma.example"


# ---------------------------------------------------------------- everything that must NOT send
def test_in_trial_mode_no_connection_is_even_opened(db, src, profile, store, cv_dir):
    application = approved(db, src, profile, dry_run=True)
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, cv_dir, dry_run=True)
    assert [o.kind for o in outcomes] == ["dry_run"] and mail.connects == []
    assert application.state == "approved" and application.dry_run_at is not None


def test_an_approval_given_in_trial_mode_is_not_honoured_when_sending_goes_live(
        db, src, profile, store, cv_dir):
    approved(db, src, profile, dry_run=True)
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, cv_dir, dry_run=False)
    assert [o.kind for o in outcomes] == ["blocked"] and mail.connects == []
    assert gate.APPROVED_IN_DRY_RUN in [r.code for r in outcomes[0].reasons]


def test_before_the_undo_window_ends_nothing_is_sent(db, src, profile, store, cv_dir):
    approved(db, src, profile)
    mail = FakeMail()
    assert run(db, profile, store, mail, cv_dir, now=NOW + timedelta(seconds=30)) == []
    assert mail.connects == []


def test_the_kill_switch_stops_everything(db, src, profile, store, cv_dir):
    approved(db, src, profile)
    profile.sending_paused = True
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, cv_dir)
    assert [o.kind for o in outcomes] == ["blocked"] and mail.connects == []
    assert gate.KILL_SWITCH in [r.code for r in outcomes[0].reasons]


def test_a_text_changed_after_the_approval_is_not_sent(db, src, profile, store, cv_dir):
    application = approved(db, src, profile)
    application.body += "\n\nP.S. dodano naknadno."  # not approved
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, cv_dir)
    assert gate.CHANGED in [r.code for r in outcomes[0].reasons] and mail.connects == []


def test_an_address_that_is_not_printed_in_the_ad_is_not_used(db, src, profile, store, cv_dir):
    application = approved(db, src, profile)
    application.to_email = "someone.else@example.org"
    application.approved_hash = service.draft_fingerprint(application)  # even if "approved"
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, cv_dir)
    assert gate.ADDRESS in [r.code for r in outcomes[0].reasons] and mail.connects == []


def test_an_address_that_was_removed_from_the_ad_is_not_used(db, src, profile, store, cv_dir):
    application = approved(db, src, profile)
    db.execute(delete(Contact).where(Contact.job_id == application.job_id))
    db.expire_all()
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, cv_dir)
    assert gate.ADDRESS in [r.code for r in outcomes[0].reasons] and mail.connects == []


def test_without_a_saved_gmail_password_nothing_can_be_sent(db, src, profile, cv_dir):
    approved(db, src, profile)
    mail = FakeMail()
    outcomes = run(db, profile, MemoryStore(), mail, cv_dir)  # no password stored
    assert gate.NOT_CONFIGURED in [r.code for r in outcomes[0].reasons] and mail.connects == []


def test_a_missing_cv_file_blocks_the_send(db, src, profile, store, tmp_path):
    approved(db, src, profile)
    mail = FakeMail()
    outcomes = run(db, profile, store, mail, tmp_path / "no-such-folder")
    assert gate.CV_MISSING in [r.code for r in outcomes[0].reasons] and mail.connects == []


def test_the_daily_cap_and_the_employer_cooldown_still_hold_with_the_real_sender(
        db, src, profile, store, cv_dir):
    profile.apply_daily_cap = 1
    approved(db, src, profile, 1, email="posao1@example.hr")
    approved(db, src, profile, 2, email="posao2@example.hr")
    mail = FakeMail()
    kinds_seen = [o.kind for o in run(db, profile, store, mail, cv_dir)]
    assert kinds_seen == ["sent", "blocked"] and len(mail.sent) == 1


# ---------------------------------------------------------------- when Gmail itself fails
def test_a_refused_password_marks_the_application_failed_and_leaks_nothing(
        db, src, profile, store, cv_dir):
    application = approved(db, src, profile)
    mail = FakeMail(login_error=auth_error())
    outcomes = run(db, profile, store, mail, cv_dir)
    assert [o.kind for o in outcomes] == ["failed"] and mail.sent == []
    assert application.state == "failed" and PASSWORD not in (application.error or "")
    assert run(db, profile, store, mail, cv_dir, now=LATER + timedelta(hours=1)) == []


def test_a_network_failure_never_leaves_a_half_state_that_could_send_twice(
        db, src, profile, store, cv_dir):
    application = approved(db, src, profile)
    mail = FakeMail(connect_error=OSError("offline"))
    assert [o.kind for o in run(db, profile, store, mail, cv_dir)] == ["failed"]
    assert application.state == "failed" and application.sent_at is None
    healthy = FakeMail()
    assert run(db, profile, store, healthy, cv_dir, now=LATER + timedelta(hours=1)) == []
    assert healthy.sent == []  # a failure is looked at by Ivan first, never retried blindly


def test_add_job_helper_gives_an_ad_address(db, src):
    assert add_job(db, src, 5).contacts[0].value == "posao@example.hr"
