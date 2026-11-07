"""Recompute scores after Ivan changes his places or profile (so the Inbox is never stale)."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.geo.mapstore import get_street_map
from app.models import Job, JobStatus, Place, Profile
from app.models.base import utcnow
from app.scoring.engine import score_job

_APP_MAP = object()  # "use the street map downloaded to this PC, if there is one"


def rescore_jobs(
    session: Session,
    profile: Profile,
    places: list[Place],
    now: datetime | None = None,
    within_days: int = 30,
    streets=_APP_MAP,
) -> int:
    """Score recent jobs again. Returns how many got a different score or status.

    An ad that the system set aside (status ignored, score 0) because it was out of reach
    comes back as `new` when the new places bring it into reach. An ad Ivan ignored himself
    keeps a score above 0 and is never revived. Backlog ads keep their `alerted_at`, so
    nothing old ever turns into an alert.
    """
    now = now or utcnow()
    streets = get_street_map() if streets is _APP_MAP else streets
    since = now - timedelta(days=within_days)
    changed = 0
    jobs = session.scalars(
        select(Job).where(Job.first_seen_at >= since, Job.duplicate_of.is_(None))
    )
    for job in jobs:
        if rescore_job(job, profile, places, now, streets):
            changed += 1
    session.flush()
    return changed


def rescore_job(job: Job, profile: Profile, places: list[Place], now: datetime, streets) -> bool:
    """Score one job again (same rules as `rescore_jobs`). True when its score or status changed."""
    result = score_job(job, profile, places, now, streets)
    before = (job.score, job.status)
    system_set_aside = job.status == JobStatus.IGNORED.value and job.score == 0
    job.score, job.score_reasons = result.score, result.reasons
    if system_set_aside and not result.excluded:
        job.status = JobStatus.NEW.value
    elif result.excluded and job.status == JobStatus.NEW.value:
        job.status = JobStatus.IGNORED.value
    return (job.score, job.status) != before
