"""Real sending (S1): the gate claims an approved application atomically, sends it exactly once with
exactly the approved text, saves at once, never retries a failure by itself and never sends again
after a crash. A recording stand-in sender; nothing real is contacted."""

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.apply import gate
from app.apply.gate import process_due
from app.core.apply_view import load_queue, sending_is_stuck
from app.core.profile import get_or_create_profile
from app.models import Application, ApplicationState, Cv, Source
from tests.test_apply_gate import LATER, Recorder, approved, go, kinds
from tests.test_apply_service import add_job

S = ApplicationState


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
    p.cvs.append(Cv(label="Hrvatski", language="hr", path="cvs/hr.pdf", is_default=True))
    db.flush()
    return p


@pytest.fixture
def cv_dir(tmp_path):
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF-1.4 test")
    return tmp_path / "cvs"


def fresh_state(db, application_id):
    """What another part of the program (another session) sees in the database right now."""
    with sessionmaker(db.get_bind())() as other:
        row = other.get(Application, application_id)
        return row.state, row.sent_at, row.message_id


# ---------------------------------------------------------------- the claim
def test_the_claim_is_won_once_and_only_by_an_approved_application(db, src, profile):
    application = approved(db, src, profile)
    assert gate._claim(db, application, LATER) is True
    assert application.state == S.SENDING.value and application.sending_at is not None
    assert gate._claim(db, application, LATER) is False  # a second run loses
    assert application.state == S.SENDING.value


def test_a_run_that_loses_the_claim_never_calls_the_sender(db, src, profile, cv_dir, monkeypatch):
    application = approved(db, src, profile)
    sender = Recorder()
    monkeypatch.setattr(gate, "_claim", lambda *args: False)
    outcome = go(db, application, profile, sender, cv_dir=cv_dir)
    assert outcome.kind == "blocked" and sender.calls == []
    assert gate.ALREADY_SENT in [r.code for r in outcome.reasons]


def test_the_state_is_saved_as_sending_before_the_message_leaves(db, src, profile, cv_dir):
    application = approved(db, src, profile)
    seen = []

    class Looks(Recorder):
        def send(self, **kw):
            seen.append(fresh_state(db, application.id)[0])  # as another session would see it
            return super().send(**kw)

    go(db, application, profile, Looks(), cv_dir=cv_dir)
    assert seen == ["sending"]


def test_a_run_started_from_inside_the_send_finds_nothing_to_send(db, src, profile, cv_dir):
    """The page and the scheduler can overlap: here a second run starts while the first one is
    in the middle of sending. It must see the claim and leave the application alone."""
    application = approved(db, src, profile)
    inner = []

    class Reentrant(Recorder):
        def send(self, **kw):
            with sessionmaker(db.get_bind())() as other:
                other_profile = get_or_create_profile(other)
                inner.append(process_due(other, other_profile, self, dry_run=False,
                                         cv_dir=cv_dir, now=LATER))
            return super().send(**kw)

    sender = Reentrant()
    assert go(db, application, profile, sender, cv_dir=cv_dir).kind == "sent"
    assert inner == [[]] and len(sender.calls) == 1


def test_two_programs_threads_at_the_same_moment_send_each_application_once(
        db, src, profile, cv_dir):
    apps = [approved(db, src, profile, n, email=f"posao{n}@example.hr") for n in (1, 2, 3)]
    db.commit()

    class Slow(Recorder):
        def send(self, **kw):
            time.sleep(0.15)
            return super().send(**kw)

    sender = Slow()
    results = []

    def worker():
        with sessionmaker(db.get_bind())() as session:
            results.append(process_due(session, get_or_create_profile(session), sender,
                                       dry_run=False, cv_dir=cv_dir, now=LATER))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(sender.calls) == 3  # three applications, three messages, however many threads
    assert len({c["to"] for c in sender.calls}) == 3  # each application exactly once
    assert all(fresh_state(db, a.id)[0] == "sent" for a in apps)


# ---------------------------------------------------------------- saved at once
def test_a_sent_application_is_saved_at_once_and_the_ad_counts_as_applied(
        db, src, profile, cv_dir):
    application = approved(db, src, profile)
    assert go(db, application, profile, Recorder(), cv_dir=cv_dir).kind == "sent"
    state, sent_at, message_id = fresh_state(db, application.id)  # without any further commit
    assert state == "sent" and sent_at is not None and message_id.startswith("<msg1@")
    assert len(kinds(db, "application_sent")) == 1


def test_a_failure_is_saved_as_failed_and_is_never_retried_by_a_later_run(
        db, src, profile, cv_dir):
    application = approved(db, src, profile)
    failing = Recorder(fail=RuntimeError("smtp password hunter2 refused"))
    assert go(db, application, profile, failing, cv_dir=cv_dir).kind == "failed"
    assert fresh_state(db, application.id)[0] == "failed"
    assert "hunter2" not in application.error and "mapu Poslano" in application.error
    again = process_due(db, profile, failing, dry_run=False, cv_dir=cv_dir,
                        now=LATER + timedelta(hours=1))
    assert again == [] and len(failing.calls) == 1


# ---------------------------------------------------------------- a crash halfway
def test_an_application_left_in_sending_is_never_sent_again_by_itself(db, src, profile, cv_dir):
    application = approved(db, src, profile)
    assert gate._claim(db, application, LATER)  # ... and the program stops right here
    sender = Recorder()
    for hours in (0, 1, 24):
        assert process_due(db, profile, sender, dry_run=False, cv_dir=cv_dir,
                           now=LATER + timedelta(hours=hours)) == []
    assert sender.calls == []
    forced = go(db, application, profile, sender, cv_dir=cv_dir)  # even asked for directly
    assert forced.kind == "blocked" and sender.calls == []


def test_a_sending_state_is_shown_as_unclear_only_after_ten_minutes(db, src, profile):
    application = approved(db, src, profile)
    assert gate._claim(db, application, LATER)
    assert not sending_is_stuck(application, LATER + timedelta(minutes=9))
    assert sending_is_stuck(application, LATER + timedelta(minutes=11))
    queue = load_queue(db, profile, now=LATER + timedelta(minutes=1))
    assert [key for key, _label, rows in queue.sections if rows] == ["approved"]
    assert "Šalje se upravo sada" in queue.sections[0][2][0].note
    stuck = load_queue(db, profile, now=LATER + timedelta(minutes=30))
    assert [key for key, _label, rows in stuck.sections if rows] == ["failed"]
    assert "Provjerite mapu Poslano" in stuck.sections[0][2][0].note


# ---------------------------------------------------------------- the batch
def test_due_applications_go_out_oldest_first_once_each(db, src, profile, cv_dir):
    first = approved(db, src, profile, 1, email="posao1@example.hr")
    second = approved(db, src, profile, 2, email="posao2@example.hr")
    second.send_after = first.send_after - timedelta(minutes=1)  # the second is older
    sender = Recorder()
    outcomes = process_due(db, profile, sender, dry_run=False, cv_dir=cv_dir, now=LATER)
    assert [o.kind for o in outcomes] == ["sent", "sent"]
    assert [c["subject"] for c in sender.calls] == [second.subject, first.subject]
    assert process_due(db, profile, sender, dry_run=False, cv_dir=cv_dir, now=LATER) == []
    assert len(sender.calls) == 2


def test_in_trial_mode_nothing_is_claimed_or_sent(db, src, profile, cv_dir):
    application = approved(db, src, profile, dry_run=True)
    sender = Recorder()
    outcomes = process_due(db, profile, sender, dry_run=True, cv_dir=cv_dir, now=LATER)
    assert [o.kind for o in outcomes] == ["dry_run"] and sender.calls == []
    assert application.state == S.APPROVED.value and application.sending_at is None


def test_add_job_helper_is_what_the_other_tests_use(db, src):
    assert add_job(db, src, 7).contacts[0].from_ad is True
