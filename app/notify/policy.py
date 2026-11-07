"""When does Ivan get an alert? (plan step 3.4)

- Score >= `notify_threshold` (default 60): alert right away.
- Score between `digest_threshold` (40) and the threshold: collected into one digest message,
  at most every `digest_interval_hours` (3).
- Below that: shown in the app only.
- Optional quiet hours (local time): urgent alerts wait until they end. Off by default, because
  the first reply usually wins the job.
- Never twice for the same ad (`alerted_at`), never for duplicates, ignored, expired or backlog ads.

This module only decides. Sending is `app/notify/dispatch.py` (step 3.2).
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Event, Job, JobStatus, Profile
from app.models.base import utcnow

LOCAL_TZ = ZoneInfo("Europe/Zagreb")


@dataclass
class NotifyPlan:
    now: list[Job] = field(default_factory=list)  # send an alert for each of these immediately
    digest: list[Job] = field(default_factory=list)  # send together as one digest message
    held: list[Job] = field(default_factory=list)  # urgent, but waiting for quiet hours to end

    @property
    def empty(self) -> bool:
        return not (self.now or self.digest)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def parse_hhmm(value: str) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except ValueError:
        raise ValueError(f"'{value}' is not a time like 22:30") from None


def in_quiet_hours(profile: Profile, now: datetime | None = None) -> bool:
    """True if the local time is inside the quiet window (which may run over midnight)."""
    if not profile.quiet_start or not profile.quiet_end:
        return False
    start, end = parse_hhmm(profile.quiet_start), parse_hhmm(profile.quiet_end)
    local = (_aware(now) or utcnow()).astimezone(LOCAL_TZ).time()
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end  # e.g. 22:00-07:00


def digest_due(profile: Profile, now: datetime | None = None) -> bool:
    last = _aware(profile.last_digest_at)
    if last is None:
        return True
    interval = timedelta(hours=profile.digest_interval_hours or 3)
    return (_aware(now) or utcnow()) - last >= interval


def eligible_jobs(session: Session, now: datetime, since: datetime | None = None) -> list[Job]:
    """Fresh, scored, not yet alerted, not a duplicate, not ignored, not expired. Best first.
    With `since`, only ads first seen at or after that moment (the time alerts were switched on:
    old ads never flood the phone)."""
    stmt = (
        select(Job)
        .where(
            Job.status == JobStatus.NEW.value,
            Job.alerted_at.is_(None),
            Job.duplicate_of.is_(None),
            Job.score.is_not(None),
            Job.score > 0,
            or_(Job.expires_at.is_(None), Job.expires_at > now),
        )
        .order_by(Job.score.desc(), Job.first_seen_at)
    )
    if since is not None:
        stmt = stmt.where(Job.first_seen_at >= since)
    return list(session.scalars(stmt))


def plan_notifications(
    session: Session, profile: Profile, now: datetime | None = None,
    since: datetime | None = None,
) -> NotifyPlan:
    now = _aware(now) or utcnow()
    plan = NotifyPlan()
    threshold = profile.notify_threshold if profile.notify_threshold is not None else 60
    digest_floor = profile.digest_threshold if profile.digest_threshold is not None else 40
    quiet = in_quiet_hours(profile, now)
    digest_ok = digest_due(profile, now)
    for job in eligible_jobs(session, now, since):
        if job.score >= threshold:
            (plan.held if quiet else plan.now).append(job)
        elif job.score >= digest_floor and digest_ok and not quiet:
            plan.digest.append(job)
    return plan


def mark_alerted(
    session: Session, jobs: list[Job], kind: str, profile: Profile | None = None,
    now: datetime | None = None,
) -> None:
    """Call after an alert was really sent: the same ad never alerts again."""
    now = _aware(now) or utcnow()
    for job in jobs:
        job.alerted_at = now
        session.add(Event(kind=f"alert_{kind}", job_id=job.id, data={"score": job.score}))
    if kind == "digest" and profile is not None:
        profile.last_digest_at = now
    session.flush()
