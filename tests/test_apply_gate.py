"""Phase 5 (P3): the approval gate. Every rule has a test that proves it blocks, and in every
blocked case the sender is never called. One positive control shows that with everything in
order the sender gets exactly the approved text, once."""

from datetime import UTC, datetime, timedelta

import pytest

from app.apply import gate, service
from app.apply.gate import send_application
from app.core.profile import get_or_create_profile
from app.models import Application, ApplicationState, Contact, Cv, Event, JobStatus, Source
from tests.test_apply_service import add_job

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=3)  # the 2-minute undo window is over
S = ApplicationState


class Recorder:
    """A stand-in sender: remembers every call, sends nothing."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[dict] = []
        self.fail = fail

    def send(self, *, to, subject, body, attachment):
        self.calls.append(dict(to=to, subject=subject, body=body, attachment=attachment))
        if self.fail:
            raise self.fail
        return f"<msg{len(self.calls)}@example.test>"


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
def sender():
    return Recorder()


def approved(db, src, profile, n=1, *, dry_run=False, **kw) -> Application:
    application = service.ensure_draft(db, add_job(db, src, n, **kw), profile, NOW)
    assert service.approve(db, application, profile, NOW, dry_run=dry_run) == {}
    return application


def go(db, application, profile, sender, *, dry_run=False, now=LATER, cv_dir=None):
    return send_application(db, application, profile, sender, dry_run=dry_run, now=now,
                            cv_dir=cv_dir)


def kinds(db, kind):
    return db.query(Event).filter(Event.kind == kind).all()


# ---------------------------------------------------------------- the positive control
def test_with_everything_in_order_the_sender_gets_exactly_the_approved_text_once(
        db, src, profile, sender, tmp_path):
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF-1.4 test")
    application = approved(db, src, profile)
    text = (application.to_email, application.subject, application.body)
    verdict = gate.check_sendable(db, application, profile, dry_run=False, now=LATER,
                                  sender_ready=True, cv_found=True)
    assert verdict.ok and verdict.reasons == []
    outcome = go(db, application, profile, sender, cv_dir=tmp_path / "cvs")
    assert outcome.kind == "sent"
    [call] = sender.calls
    assert (call["to"], call["subject"], call["body"]) == text
    assert call["attachment"] == (tmp_path / "cvs" / "hr.pdf").resolve()
    assert application.state == S.SENT.value and application.sent_at == LATER
    assert application.message_id == "<msg1@example.test>" and application.error is None
    assert application.job.status == JobStatus.APPLIED.value
    assert len(kinds(db, "application_sent")) == 1 and len(kinds(db, "job_applied")) == 1
    # asking again never sends twice
    again = go(db, application, profile, sender, cv_dir=tmp_path / "cvs",
               now=LATER + timedelta(hours=1))
    assert again.kind == "blocked" and gate.ALREADY_SENT in [r.code for r in again.reasons]
    assert len(sender.calls) == 1


# ------------ the hard rule: approval of THIS text
def test_without_an_approval_the_sender_is_never_called(db, src, profile, sender):
    application = service.ensure_draft(db, add_job(db, src), profile, NOW)  # waits for approval
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and sender.calls == []
    codes = [r.code for r in outcome.reasons]
    assert gate.NO_APPROVAL in codes and gate.WRONG_STATE in codes
    assert application.sent_at is None and application.state == S.QUEUED.value


def test_even_a_forced_state_without_an_approval_stays_blocked(db, src, profile, sender):
    application = service.ensure_draft(db, add_job(db, src), profile, NOW)
    application.state = S.APPROVED.value  # someone edits the database: no approval was ever given
    application.send_after = NOW
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and gate.NO_APPROVAL in [r.code for r in outcome.reasons]
    assert sender.calls == []


@pytest.mark.parametrize("what", ["body", "subject", "to_email", "cv_id"])
def test_a_draft_changed_after_the_approval_is_never_sent(db, src, profile, sender, what):
    application = approved(db, src, profile)
    if what == "body":
        application.body += "\nP.S. Mogu početi odmah."
    elif what == "subject":
        application.subject += "!"
    elif what == "to_email":
        application.to_email = "other@example.hr"
    else:
        application.cv_id = None
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and gate.CHANGED in [r.code for r in outcome.reasons]
    assert sender.calls == []


def test_an_edit_goes_through_the_service_only_after_taking_the_approval_back(
        db, src, profile, sender):
    application = approved(db, src, profile)
    assert service.edit_draft(db, application, profile, subject="N", body="T",
                              cv_id=application.cv_id) != {}
    assert service.undo(db, application, NOW + timedelta(seconds=30)) is None
    assert service.edit_draft(db, application, profile, subject="Novi naslov",
                              body="Poštovani,\nnovi tekst\nIvan", cv_id=application.cv_id) == {}
    assert go(db, application, profile, sender).kind == "blocked"  # not approved any more
    assert sender.calls == []


# ---------------------------------------------------------------- the undo window
def test_inside_the_undo_window_nothing_is_sent_and_at_its_end_it_may_go(db, src, profile,
                                                                         sender):
    application = approved(db, src, profile)
    early = go(db, application, profile, sender, now=NOW + timedelta(seconds=119))
    assert early.kind == "blocked" and gate.UNDO_WINDOW in [r.code for r in early.reasons]
    assert sender.calls == []
    on_time = go(db, application, profile, sender, now=NOW + timedelta(minutes=2))
    assert on_time.kind in ("failed", "blocked", "sent")
    assert gate.UNDO_WINDOW not in [r.code for r in on_time.reasons]


# ---------------------------------------------------------------- the address
def test_an_address_that_is_not_printed_in_the_ad_is_never_used(db, src, profile, sender):
    application = approved(db, src, profile)
    for contact in list(application.job.contacts):
        contact.from_ad = False  # the address came from somewhere else
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and gate.ADDRESS in [r.code for r in outcome.reasons]
    assert sender.calls == []


@pytest.mark.parametrize("to", ["a@example.hr,b@example.hr", "a@example.hr;b@example.hr",
                                "Name <a@example.hr>", "a@example.hr\r\nBcc: x@example.hr",
                                "a b@example.hr", "no-at-sign"])
def test_an_address_that_could_add_recipients_or_headers_is_never_used(db, src, profile, sender,
                                                                      to):
    application = approved(db, src, profile)
    # the draft service would never accept such an address; a database edit forces it in, with a
    # matching approval, to prove the gate catches it on its own
    application.to_email = to
    application.approved_hash = service.draft_fingerprint(application)
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and sender.calls == []


def test_a_subject_with_a_line_break_is_never_sent_even_if_it_was_approved_that_way(
        db, src, profile, sender):
    application = approved(db, src, profile)
    application.subject = "Prijava\r\nBcc: x@example.hr"
    application.approved_hash = service.draft_fingerprint(application)  # approval matches it
    outcome = go(db, application, profile, sender)
    assert gate.UNSAFE_HEADER in [r.code for r in outcome.reasons] and sender.calls == []


# ---------------------------------------------------------------- the ad, the kill switch
@pytest.mark.parametrize("status", [JobStatus.IGNORED.value, JobStatus.APPLIED.value,
                                    JobStatus.SNOOZED.value])
def test_an_ad_that_is_no_longer_open_gets_nothing_sent(db, src, profile, sender, status):
    application = approved(db, src, profile)
    application.job.status = status
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and gate.JOB_CLOSED in [r.code for r in outcome.reasons]
    assert sender.calls == []


def test_the_kill_switch_stops_everything_and_releasing_it_lets_the_same_draft_go(
        db, src, profile, sender, tmp_path):
    application = approved(db, src, profile)
    service.set_paused(db, profile, True)
    stopped = go(db, application, profile, sender, cv_dir=tmp_path / "cvs")
    assert stopped.kind == "blocked" and gate.KILL_SWITCH in [r.code for r in stopped.reasons]
    assert sender.calls == [] and application.state == S.APPROVED.value
    service.set_paused(db, profile, False)
    verdict = gate.check_sendable(db, application, profile, dry_run=False, now=LATER,
                                  cv_found=True)
    assert gate.KILL_SWITCH not in verdict.codes


# ---------------------------------------------------------------- dry run
def test_in_dry_run_a_send_that_would_be_allowed_is_only_logged(db, src, profile, sender):
    application = approved(db, src, profile, dry_run=True)
    verdict = gate.check_sendable(db, application, profile, dry_run=True, now=LATER)
    assert verdict.codes == [gate.DRY_RUN] and verdict.dry_run_only and not verdict.ok
    outcome = go(db, application, profile, sender, dry_run=True)
    assert outcome.kind == "dry_run" and sender.calls == []
    assert application.dry_run_at == LATER and application.sent_at is None
    assert application.state == S.APPROVED.value  # not sent, not pretending to be
    assert application.job.status == JobStatus.NEW.value
    [event] = kinds(db, "application_dry_run")
    assert event.data == {"application": application.id, "to": application.to_email}


def test_dry_run_does_not_hide_other_problems(db, src, profile, sender):
    application = service.ensure_draft(db, add_job(db, src), profile, NOW)  # not approved
    verdict = gate.check_sendable(db, application, profile, dry_run=True, now=LATER)
    assert gate.NO_APPROVAL in verdict.codes and gate.DRY_RUN in verdict.codes
    assert not verdict.dry_run_only
    assert go(db, application, profile, sender, dry_run=True).kind == "blocked"
    assert kinds(db, "application_dry_run") == []


def test_an_approval_given_in_dry_run_is_not_honoured_once_real_sending_is_on(
        db, src, profile, sender):
    application = approved(db, src, profile, dry_run=True)
    go(db, application, profile, sender, dry_run=True)  # the dry run logged it
    now_real = go(db, application, profile, sender, dry_run=False, now=LATER + timedelta(days=1))
    assert now_real.kind == "blocked"
    assert gate.APPROVED_IN_DRY_RUN in [r.code for r in now_real.reasons]
    assert sender.calls == []
    # Ivan takes that approval back and approves the text again, in real mode
    assert service.release_dry_run_approval(db, application) is None
    assert application.state == S.QUEUED.value and application.dry_run_at is None
    assert service.approve(db, application, profile, NOW + timedelta(days=1), dry_run=False) == {}
    verdict = gate.check_sendable(db, application, profile, dry_run=False,
                                  now=NOW + timedelta(days=1, minutes=3), cv_found=True)
    assert verdict.ok


def test_only_a_dry_run_approval_can_be_released(db, src, profile):
    application = approved(db, src, profile, dry_run=False)
    assert service.release_dry_run_approval(db, application) is not None
    assert application.state == S.APPROVED.value


# ---------------------------------------------------------------- the daily cap
def sent_before(db, src, n, *, when, to=None, employer=None, dry=False):
    other = service.ensure_draft(db, add_job(db, src, n, employer=employer or f"Firma {n}"),
                                 get_or_create_profile(db), NOW)
    if to:
        other.to_email = to
    if dry:
        other.dry_run_at = when
    else:
        other.sent_at, other.state = when, S.SENT.value
    db.flush()
    return other


def test_the_daily_cap_blocks_the_application_after_the_limit_is_reached(db, src, profile, sender):
    profile.apply_daily_cap = 2
    for n in (10, 11):
        sent_before(db, src, n, when=NOW + timedelta(minutes=n))
    application = approved(db, src, profile, n=1)
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and gate.DAILY_CAP in [r.code for r in outcome.reasons]
    assert sender.calls == []


def test_sends_of_an_earlier_day_do_not_count_towards_todays_cap(db, src, profile, sender):
    profile.apply_daily_cap = 1
    sent_before(db, src, 10, when=NOW - timedelta(days=1))  # yesterday, Croatian time
    application = approved(db, src, profile, n=1)
    verdict = gate.check_sendable(db, application, profile, dry_run=False, now=LATER,
                                  cv_found=True)
    assert gate.DAILY_CAP not in verdict.codes


def test_the_cap_day_starts_at_croatian_midnight(db, src, profile):
    profile.apply_daily_cap = 1
    # 23:30 on 18 Oct Croatian time (UTC+2) is 21:30 UTC; "now" is 00:30 Croatian time on 19 Oct
    sent_before(db, src, 10, when=datetime(2026, 10, 18, 21, 30, tzinfo=UTC))
    application = approved(db, src, profile, n=1)
    now = datetime(2026, 10, 18, 22, 30, tzinfo=UTC)
    verdict = gate.check_sendable(db, application, profile, dry_run=False, now=now, cv_found=True)
    assert gate.DAILY_CAP not in verdict.codes


def test_logged_dry_runs_count_towards_the_cap_only_while_in_dry_run(db, src, profile):
    profile.apply_daily_cap = 1
    sent_before(db, src, 10, when=NOW + timedelta(minutes=1), dry=True)
    application = approved(db, src, profile, n=1, dry_run=True)
    in_dry = gate.check_sendable(db, application, profile, dry_run=True, now=LATER)
    assert gate.DAILY_CAP in in_dry.codes  # a dry-run day shows what the cap would have done
    application.dry_run = False
    real = gate.check_sendable(db, application, profile, dry_run=False, now=LATER, cv_found=True)
    assert gate.DAILY_CAP not in real.codes  # nothing really went out


# ---------------------------------------------------------------- the per-employer cooldown
def test_the_same_address_is_not_written_to_again_inside_the_cooldown(db, src, profile, sender):
    sent_before(db, src, 10, when=NOW - timedelta(days=5), to="posao@example.hr",
                employer="Sasvim druga firma")
    application = approved(db, src, profile, n=1)
    outcome = go(db, application, profile, sender)
    assert outcome.kind == "blocked" and gate.COOLDOWN in [r.code for r in outcome.reasons]
    assert sender.calls == []


def test_the_same_employer_under_another_address_is_covered_too(db, src, profile, sender):
    sent_before(db, src, 10, when=NOW - timedelta(days=5), to="drugi@example.hr",
                employer="Firma 1 d.o.o.")
    application = approved(db, src, profile, n=1)  # employer "Firma 1 d.o.o."
    assert gate.COOLDOWN in gate.check_sendable(db, application, profile, dry_run=False,
                                                now=LATER, cv_found=True).codes


def test_after_the_cooldown_or_for_another_employer_the_message_may_go(db, src, profile):
    sent_before(db, src, 10, when=NOW - timedelta(days=15), to="posao@example.hr",
                employer="Firma 1 d.o.o.")  # 15 days ago: over the 14
    sent_before(db, src, 11, when=NOW - timedelta(days=1), to="treci@example.hr",
                employer="Treća firma")  # yesterday, but somebody else
    application = approved(db, src, profile, n=1)
    verdict = gate.check_sendable(db, application, profile, dry_run=False, now=LATER,
                                  cv_found=True)
    assert gate.COOLDOWN not in verdict.codes


def test_a_cooldown_of_zero_days_switches_the_rule_off(db, src, profile):
    profile.apply_cooldown_days = 0
    sent_before(db, src, 10, when=NOW - timedelta(minutes=10), to="posao@example.hr")
    application = approved(db, src, profile, n=1)
    assert gate.COOLDOWN not in gate.check_sendable(db, application, profile, dry_run=False,
                                                    now=LATER, cv_found=True).codes


def test_the_cooldown_looks_at_the_address_case_insensitively(db, src, profile):
    sent_before(db, src, 10, when=NOW - timedelta(days=2), to="POSAO@EXAMPLE.HR",
                employer="Neka firma")
    application = approved(db, src, profile, n=1)
    assert gate.COOLDOWN in gate.check_sendable(db, application, profile, dry_run=False,
                                                now=LATER, cv_found=True).codes


# ---------------------------------------------------------------- a sender must exist
def test_without_a_sender_in_real_mode_nothing_happens(db, src, profile):
    application = approved(db, src, profile)
    outcome = send_application(db, application, profile, None, dry_run=False, now=LATER)
    assert outcome.kind == "blocked" and gate.NOT_CONFIGURED in [r.code for r in outcome.reasons]
    assert application.state == S.APPROVED.value and application.sent_at is None


def test_a_missing_cv_file_blocks_a_real_send(db, src, profile, sender, tmp_path):
    application = approved(db, src, profile)  # CV "cvs/hr.pdf" is not on disk
    outcome = go(db, application, profile, sender, cv_dir=tmp_path / "cvs")
    assert outcome.kind == "blocked" and gate.CV_MISSING in [r.code for r in outcome.reasons]
    assert sender.calls == []


def test_a_cv_path_that_leaves_the_cv_folder_is_not_attached(db, src, profile, sender, tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    cv = profile.cvs[0]
    cv.path = "../secret.txt"
    application = approved(db, src, profile)
    cv_root = tmp_path / "cvs"
    cv_root.mkdir()
    outcome = go(db, application, profile, sender, cv_dir=cv_root)
    assert outcome.kind == "blocked" and gate.CV_MISSING in [r.code for r in outcome.reasons]
    assert sender.calls == []


# ---------------------------------------------------------------- a failing sender
def test_a_failing_sender_marks_the_application_failed_without_leaking_text(
        db, src, profile, tmp_path):
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF")
    application = approved(db, src, profile)
    failing = Recorder(fail=RuntimeError("smtp password hunter2 refused"))
    outcome = go(db, application, profile, failing, cv_dir=tmp_path / "cvs")
    assert outcome.kind == "failed" and len(failing.calls) == 1
    assert application.state == S.FAILED.value and application.sent_at is None
    assert application.error.startswith("Slanje nije uspjelo (RuntimeError).")
    assert "mapu Poslano u Gmailu" in application.error  # it may have left: look before retrying
    assert "hunter2" not in application.error
    [event] = kinds(db, "application_failed")
    assert event.data == {"application": application.id, "error": "RuntimeError"}
    assert application.job.status == JobStatus.NEW.value  # the ad is still open
    retry = go(db, application, profile, failing, cv_dir=tmp_path / "cvs",
               now=LATER + timedelta(hours=1))
    assert retry.kind == "blocked" and len(failing.calls) == 1  # never retried blindly


# ---------------------------------------------------------------- every reason at once, and the log
def test_all_the_reasons_are_listed_together_in_plain_croatian(db, src, profile):
    application = service.ensure_draft(db, add_job(db, src), profile, NOW)
    application.job.status = JobStatus.IGNORED.value
    service.set_paused(db, profile, True)
    verdict = gate.check_sendable(db, application, profile, dry_run=True, now=NOW)
    assert set(verdict.codes) >= {gate.WRONG_STATE, gate.NO_APPROVAL, gate.UNDO_WINDOW,
                                  gate.JOB_CLOSED, gate.KILL_SWITCH, gate.DRY_RUN}
    assert all(r.text.endswith(".") and r.text[0].isupper() for r in verdict.reasons)


def test_a_blocked_application_is_logged_once_per_change_of_reason_not_on_every_tick(
        db, src, profile, sender):
    application = approved(db, src, profile)
    service.set_paused(db, profile, True)
    for minute in range(4, 9):
        go(db, application, profile, sender, now=NOW + timedelta(minutes=minute))
    assert len(kinds(db, "application_blocked")) == 1
    assert "zaustavljeno" in application.error
    service.set_paused(db, profile, False)
    profile.apply_daily_cap = 1
    sent_before(db, src, 10, when=NOW + timedelta(minutes=1))
    go(db, application, profile, sender, now=NOW + timedelta(minutes=9))
    assert len(kinds(db, "application_blocked")) == 2  # the reason changed: logged again


# ---------------------------------------------------------------- doing the due ones
def test_process_due_handles_only_approved_applications_whose_window_is_over(
        db, src, profile, sender, tmp_path):
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF")
    profile.apply_daily_cap = 10
    ready = approved(db, src, profile, n=1)
    waiting = approved(db, src, profile, n=2)
    waiting.send_after = LATER + timedelta(hours=1)  # window still open
    unapproved = service.ensure_draft(db, add_job(db, src, 3), profile, NOW)
    outcomes = gate.process_due(db, profile, sender, dry_run=False, cv_dir=tmp_path / "cvs",
                                now=LATER)
    assert [o.kind for o in outcomes] == ["sent"] and len(sender.calls) == 1
    assert ready.state == S.SENT.value and waiting.state == S.APPROVED.value
    assert unapproved.state == S.QUEUED.value


def test_process_due_in_dry_run_logs_each_once_and_sends_nothing(db, src, profile, sender):
    profile.apply_daily_cap = 10
    first = approved(db, src, profile, n=1, dry_run=True, email="a@example.hr")
    second = approved(db, src, profile, n=2, dry_run=True, email="b@example.hr")
    outcomes = gate.process_due(db, profile, sender, dry_run=True, now=LATER)
    assert [o.kind for o in outcomes] == ["dry_run", "dry_run"] and sender.calls == []
    later = LATER + timedelta(minutes=5)
    assert gate.process_due(db, profile, sender, dry_run=True, now=later) == []
    assert len(kinds(db, "application_dry_run")) == 2
    assert first.dry_run_at and second.dry_run_at


def test_process_due_respects_its_limit_and_goes_oldest_first(db, src, profile, sender):
    profile.apply_daily_cap = 50
    apps = [approved(db, src, profile, n=n, dry_run=True, email=f"p{n}@example.hr")
            for n in range(1, 5)]
    for offset, application in zip((3, 1, 2, 0), apps, strict=True):
        application.send_after = NOW + timedelta(minutes=offset)
    outcomes = gate.process_due(db, profile, sender, dry_run=True, now=LATER, limit=2)
    assert len(outcomes) == 2
    logged = [e.data["application"] for e in db.query(Event).order_by(Event.id)
              if e.kind == "application_dry_run"]
    assert logged == [apps[3].id, apps[1].id]  # send_after 0 min, then 1 min


# ------------ the sender is never called when blocked
@pytest.mark.parametrize("spoil", [
    lambda a, p, db: setattr(a, "approved_hash", None),
    lambda a, p, db: setattr(a, "approved_at", None),
    lambda a, p, db: setattr(a, "body", a.body + " "),
    lambda a, p, db: setattr(a, "state", ApplicationState.QUEUED.value),
    lambda a, p, db: setattr(a, "state", ApplicationState.SKIPPED.value),
    lambda a, p, db: setattr(a, "send_after", LATER + timedelta(minutes=1)),
    lambda a, p, db: setattr(a.job, "status", JobStatus.IGNORED.value),
    lambda a, p, db: setattr(p, "sending_paused", True),
    lambda a, p, db: setattr(a, "dry_run", True),
    lambda a, p, db: [setattr(c, "from_ad", False) for c in a.job.contacts],
    lambda a, p, db: (db.query(Contact).delete(), db.expire_all()),
    lambda a, p, db: setattr(a, "sent_at", NOW),
], ids=["hash gone", "approved_at gone", "text changed", "back to queued", "skipped",
        "window open", "ad ignored", "kill switch", "approved in dry-run", "address not from ad",
        "address gone", "already sent"])
def test_whatever_is_spoiled_the_sender_is_never_called(db, src, profile, sender, tmp_path, spoil):
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF")
    application = approved(db, src, profile)
    spoil(application, profile, db)
    outcome = go(db, application, profile, sender, cv_dir=tmp_path / "cvs")
    assert outcome.kind == "blocked" and sender.calls == []
