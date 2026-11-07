"""Step 4.3 (logic): ignore / snooze / applied / bring back. Each changes the status AND logs."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.job_actions import (
    ACTIONS,
    ActionError,
    apply_action,
    available_actions,
    mark_seen,
    snooze_until,
    wake_due_snoozed,
)
from app.models import Event, Job, Source

NOW = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)  # 22:00 in Croatia (summer time)


def make_job(db, status="seen", n=1):
    src = db.query(Source).first() or Source(key="t", name="Test")
    db.add(src)
    job = Job(source=src, external_id=f"e{n}", url="https://example.hr/x", title=f"Posao {n}",
              status=status, first_seen_at=NOW)
    db.add(job)
    db.flush()
    return job


def events(db, job):
    return db.query(Event).filter(Event.job_id == job.id).order_by(Event.id).all()


# ---------------------------------------------------------------- which buttons
@pytest.mark.parametrize("status, expected", [
    ("new", ("zanemari", "odgodi", "prijavljeno")),
    ("seen", ("zanemari", "odgodi", "prijavljeno")),
    ("snoozed", ("vrati", "zanemari", "prijavljeno")),
    ("ignored", ("vrati",)),
    ("applied", ("vrati",)),
])
def test_buttons_that_make_sense_for_each_status(status, expected):
    assert available_actions(status) == expected
    assert set(expected) <= set(ACTIONS)


# ---------------------------------------------------------------- snooze times
def test_snooze_three_hours():
    assert snooze_until("3h", NOW) == NOW + timedelta(hours=3)


def test_snooze_until_tomorrow_8_croatian_time():
    # 22:00 local on the 17th (CEST, UTC+2) -> 08:00 local on the 18th = 06:00 UTC
    assert snooze_until("sutra", NOW) == datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
    # just after midnight local: "tomorrow" is the next calendar day, not 8 hours later
    late = datetime(2026, 10, 17, 22, 30, tzinfo=UTC)  # 00:30 local on the 18th
    assert snooze_until("sutra", late) == datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


def test_snooze_tomorrow_follows_the_clock_change():
    # clocks go back on 2026-10-25 at 03:00: 08:00 that morning is UTC+1 = 07:00 UTC
    saturday_noon = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert snooze_until("sutra", saturday_noon) == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)


def test_unknown_snooze_choice_is_refused():
    with pytest.raises(ActionError):
        snooze_until("never", NOW)


# ---------------------------------------------------------------- actions change state and log
def test_ignore(db):
    job = make_job(db)
    assert apply_action(db, job, "zanemari", now=NOW) == "zanemari"
    assert job.status == "ignored" and job.snoozed_until is None
    [event] = events(db, job)
    assert (event.kind, event.data) == ("job_ignored", {"from": "seen"})


def test_snooze_sets_the_time_and_logs_it(db):
    job = make_job(db, status="new")
    apply_action(db, job, "odgodi", snooze="sutra", now=NOW)
    assert job.status == "snoozed"
    assert job.snoozed_until.replace(tzinfo=UTC) == datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
    [event] = events(db, job)
    assert event.kind == "job_snoozed"
    assert event.data == {"from": "new", "until": "2026-10-18T06:00:00+00:00"}


def test_applied(db):
    job = make_job(db)
    apply_action(db, job, "prijavljeno", now=NOW)
    assert job.status == "applied"
    assert [(e.kind, e.data) for e in events(db, job)] == [("job_applied", {"from": "seen"})]


@pytest.mark.parametrize("status", ["ignored", "applied", "snoozed"])
def test_bring_back_puts_it_among_the_open_ones_and_clears_the_snooze(db, status):
    job = make_job(db, status=status)
    job.snoozed_until = NOW + timedelta(hours=1) if status == "snoozed" else None
    apply_action(db, job, "vrati", now=NOW)
    assert job.status == "seen" and job.snoozed_until is None
    [event] = events(db, job)
    assert (event.kind, event.data) == ("job_reopened", {"from": status})


def test_every_action_writes_exactly_one_event_for_that_job(db):
    job, other = make_job(db, n=1), make_job(db, n=2)
    apply_action(db, job, "odgodi", now=NOW)
    apply_action(db, job, "zanemari", now=NOW)
    apply_action(db, job, "vrati", now=NOW)
    apply_action(db, job, "prijavljeno", now=NOW)
    assert [e.kind for e in events(db, job)] == [
        "job_snoozed", "job_ignored", "job_reopened", "job_applied"]
    assert events(db, other) == []


# ---------------------------------------------------------------- refused requests change nothing
@pytest.mark.parametrize("action", ["", "delete", "ZANEMARI", "zanemari ", "<script>"])
def test_unknown_action_is_refused(db, action):
    job = make_job(db)
    with pytest.raises(ActionError):
        apply_action(db, job, action, now=NOW)
    assert job.status == "seen" and events(db, job) == []


def test_action_that_does_not_fit_the_state_is_refused(db):
    job = make_job(db, status="ignored")
    for action in ("zanemari", "odgodi", "prijavljeno"):
        with pytest.raises(ActionError):
            apply_action(db, job, action, now=NOW)
    assert job.status == "ignored" and events(db, job) == []


def test_bad_snooze_choice_changes_nothing(db):
    job = make_job(db)
    with pytest.raises(ActionError):
        apply_action(db, job, "odgodi", snooze="forever", now=NOW)
    assert job.status == "seen" and job.snoozed_until is None


# ---------------------------------------------------------------- looking at an ad
def test_opening_a_new_ad_marks_it_seen_and_nothing_else(db):
    new, ignored = make_job(db, "new", 1), make_job(db, "ignored", 2)
    assert mark_seen(db, new) is True and new.status == "seen"
    assert mark_seen(db, new) is False
    assert mark_seen(db, ignored) is False and ignored.status == "ignored"
    assert events(db, new) == []


# ---------------------------------------------------------------- waking up
def test_due_snoozed_ads_come_back_and_others_stay(db):
    due = make_job(db, "snoozed", 1)
    due.snoozed_until = NOW - timedelta(minutes=1)
    later = make_job(db, "snoozed", 2)
    later.snoozed_until = NOW + timedelta(hours=2)
    forever = make_job(db, "snoozed", 3)  # snoozed without a time (old data): stays put
    plain = make_job(db, "seen", 4)
    assert wake_due_snoozed(db, NOW) == 1
    assert (due.status, due.snoozed_until) == ("seen", None)
    assert [j.status for j in (later, forever, plain)] == ["snoozed", "snoozed", "seen"]
    assert [(e.kind, e.data) for e in events(db, due)] == [("job_unsnoozed", {"from": "snoozed"})]
    assert events(db, later) == [] and events(db, plain) == []
    assert wake_due_snoozed(db, NOW) == 0  # nothing happens twice


# ---------------------------------------------------------------- the phone button (plan 5.5)
def test_the_call_button_is_offered_only_for_phone_ads():
    assert "nazvano" in available_actions("seen", "phone")
    assert "nazvano" in available_actions("new", "phone")
    assert "nazvano" in available_actions("snoozed", "phone")
    for channel in ("email", "site", "none", None):
        assert "nazvano" not in available_actions("seen", channel)
    assert available_actions("ignored", "phone") == ("vrati",)
    assert available_actions("applied", "phone") == ("vrati",)


def test_called_marks_the_ad_as_applied_and_logs_a_call(db):
    job = make_job(db)
    job.channel = "phone"
    assert apply_action(db, job, "nazvano", now=NOW) == "nazvano"
    assert job.status == "applied" and job.snoozed_until is None
    [event] = events(db, job)
    assert event.kind == "job_called" and event.data == {"from": "seen"}


def test_called_is_refused_for_an_ad_with_another_way_to_apply_and_changes_nothing(db):
    job = make_job(db)
    job.channel = "email"
    with pytest.raises(ActionError):
        apply_action(db, job, "nazvano", now=NOW)
    assert job.status == "seen" and events(db, job) == []
