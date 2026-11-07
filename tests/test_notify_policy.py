"""Step 3.4: when Ivan gets an alert."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.profile import get_or_create_profile
from app.models import Event, Job, Source
from app.notify.policy import (
    digest_due,
    in_quiet_hours,
    mark_alerted,
    parse_hhmm,
    plan_notifications,
)

# 2026-10-15 is summer time in Croatia: local = UTC + 2
NOON_UTC = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)  # 12:00 local
NIGHT_UTC = datetime(2026, 10, 15, 21, 30, tzinfo=UTC)  # 23:30 local
EARLY_UTC = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)  # 05:00 local


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.notify_threshold, p.digest_threshold, p.digest_interval_hours = 60, 40, 3
    db.commit()
    return p


def add(db, score, *, title=None, status="new", alerted=False, dup=None, expires=None, ext=None):
    s = db.query(Source).filter_by(key="n").first() or Source(key="n", name="n")
    n = db.query(Job).count() + 1
    j = Job(source=s, external_id=ext or f"j{n}", url=f"https://x/{n}", title=title or f"Posao {n}",
            score=score, status=status, first_seen_at=NOON_UTC, expires_at=expires,
            alerted_at=NOON_UTC if alerted else None, duplicate_of=dup)
    db.add(j)
    db.commit()
    return j


def titles(jobs):
    return [j.title for j in jobs]


# ---------- thresholds
def test_good_ads_alert_now_best_first(db, profile):
    add(db, 70, title="B")
    add(db, 95, title="A")
    plan = plan_notifications(db, profile, NOON_UTC)
    assert titles(plan.now) == ["A", "B"] and plan.digest == [] and plan.held == []


def test_middle_scores_go_to_the_digest_not_to_an_instant_alert(db, profile):
    add(db, 59, title="mid")
    add(db, 45, title="low-mid")
    plan = plan_notifications(db, profile, NOON_UTC)
    assert plan.now == [] and titles(plan.digest) == ["mid", "low-mid"]


def test_low_scores_are_only_shown_in_the_app(db, profile):
    add(db, 39)
    plan = plan_notifications(db, profile, NOON_UTC)
    assert plan.empty and plan.held == []


def test_threshold_boundaries(db, profile):
    add(db, 60, title="exactly-60")
    add(db, 40, title="exactly-40")
    plan = plan_notifications(db, profile, NOON_UTC)
    assert titles(plan.now) == ["exactly-60"] and titles(plan.digest) == ["exactly-40"]


def test_thresholds_come_from_the_profile(db, profile):
    profile.notify_threshold = 80
    db.commit()
    add(db, 75)
    plan = plan_notifications(db, profile, NOON_UTC)
    assert plan.now == [] and len(plan.digest) == 1


# ---------- never alert for these
@pytest.mark.parametrize(
    "kw",
    [
        {"alerted": True},  # already alerted once
        {"status": "ignored"},  # outside the radius / Ivan ignored it
        {"status": "seen"},  # backlog from the first crawl, or already looked at
        {"status": "applied"},
        {"expires": NOON_UTC - timedelta(hours=1)},  # deadline has passed
    ],
)
def test_ads_that_must_not_alert(db, profile, kw):
    add(db, 95, **kw)
    assert plan_notifications(db, profile, NOON_UTC).empty


def test_duplicates_never_alert(db, profile):
    original = add(db, 95)
    add(db, 95, dup=original.id)
    plan = plan_notifications(db, profile, NOON_UTC)
    assert len(plan.now) == 1 and plan.now[0].id == original.id


def test_unscored_or_zero_score_ads_never_alert(db, profile):
    add(db, None)
    add(db, 0)
    assert plan_notifications(db, profile, NOON_UTC).empty


def test_ad_with_a_future_deadline_still_alerts(db, profile):
    add(db, 80, expires=NOON_UTC + timedelta(days=2))
    assert len(plan_notifications(db, profile, NOON_UTC).now) == 1


# ---------- once only
def test_marking_as_alerted_stops_repeats_and_logs_an_event(db, profile):
    job = add(db, 90)
    mark_alerted(db, plan_notifications(db, profile, NOON_UTC).now, "now", now=NOON_UTC)
    db.commit()
    assert plan_notifications(db, profile, NOON_UTC).empty
    assert db.query(Event).filter_by(kind="alert_now", job_id=job.id).count() == 1


# ---------- digest timing
def test_digest_waits_for_its_interval(db, profile):
    add(db, 50, title="mid")
    plan = plan_notifications(db, profile, NOON_UTC)
    mark_alerted(db, plan.digest, "digest", profile, NOON_UTC)
    db.commit()
    add(db, 55, title="later")
    soon = plan_notifications(db, profile, NOON_UTC + timedelta(hours=1))
    assert soon.digest == []  # only 1 h since the last digest
    later = plan_notifications(db, profile, NOON_UTC + timedelta(hours=3, minutes=1))
    assert titles(later.digest) == ["later"]


def test_digest_is_due_the_first_time(db, profile):
    assert profile.last_digest_at is None and digest_due(profile, NOON_UTC)


def test_instant_alerts_do_not_wait_for_the_digest_timer(db, profile):
    mark_alerted(db, [], "digest", profile, NOON_UTC)  # a digest just went out
    add(db, 92)
    assert len(plan_notifications(db, profile, NOON_UTC + timedelta(minutes=5)).now) == 1


# ---------- quiet hours
def set_quiet(db, profile, start, end):
    profile.quiet_start, profile.quiet_end = start, end
    db.commit()


def test_quiet_hours_are_off_by_default(db, profile):
    assert not in_quiet_hours(profile, NIGHT_UTC)


@pytest.mark.parametrize(
    "when,expected",
    [(NIGHT_UTC, True), (EARLY_UTC, True), (NOON_UTC, False)],
)
def test_overnight_quiet_window_22_to_07(db, profile, when, expected):
    set_quiet(db, profile, "22:00", "07:00")
    assert in_quiet_hours(profile, when) is expected


def test_same_day_quiet_window(db, profile):
    set_quiet(db, profile, "13:00", "15:00")
    assert not in_quiet_hours(profile, NOON_UTC)  # 12:00 local
    assert in_quiet_hours(profile, NOON_UTC + timedelta(hours=2))  # 14:00 local


def test_urgent_alerts_are_held_during_quiet_hours_and_released_after(db, profile):
    set_quiet(db, profile, "22:00", "07:00")
    add(db, 90, title="urgent")
    add(db, 50, title="digest-material")
    night = plan_notifications(db, profile, NIGHT_UTC)
    assert night.now == [] and night.digest == [] and titles(night.held) == ["urgent"]
    morning = plan_notifications(db, profile, datetime(2026, 10, 16, 5, 30, tzinfo=UTC))  # 07:30
    assert titles(morning.now) == ["urgent"] and titles(morning.digest) == ["digest-material"]


def test_empty_start_or_end_means_no_quiet_hours(db, profile):
    set_quiet(db, profile, "22:00", None)
    assert not in_quiet_hours(profile, NIGHT_UTC)


def test_bad_time_text_is_rejected_with_a_plain_message():
    with pytest.raises(ValueError, match="22:30"):
        parse_hhmm("late evening")


def test_naive_datetimes_from_sqlite_are_read_as_utc(db, profile):
    profile.last_digest_at = (NOON_UTC - timedelta(hours=1)).replace(tzinfo=None)
    assert not digest_due(profile, NOON_UTC)
