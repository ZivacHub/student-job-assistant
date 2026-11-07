"""Check once how exact the location of every job stored before step 4.10 is (D40).

Before 4.10 the app kept only lat/lon, so an ad that names just the town (job "TC Portanova",
Osijek) sat at the town centre and got street minutes as if it were exact. This marks each old
job "city" (its point is the town-table point) or "address", looks up the place named in the title
for the "city" ones ("Portanova, Osijek" on OpenStreetMap, 1 request per second, cached), and
scores the job again. A job set aside by the old wrong minutes may come back among the open ones,
but it never turns into an alert (its `alerted_at` is set), so this makes no email or push.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.profile import get_or_create_profile
from app.geo.cities import lookup_city
from app.geo.mapstore import get_street_map
from app.geo.nominatim import Geocoder
from app.geo.placenames import place_names
from app.models import Event, Job, JobStatus, Place, Profile
from app.models.base import utcnow
from app.scoring.rescore import rescore_job

log = logging.getLogger(__name__)

BATCH = 50  # jobs per run; the scheduler comes back until none are left
RESCORE_WITHIN_DAYS = 30  # the same window as `rescore_jobs`
SAME_SPOT = 1e-4  # degrees: a point this close to the town-table point IS the town-table point


def _pending(column=Job.id):
    """Select the ads stored before step 4.10 that have a location but no precision yet."""
    return select(column).where(
        Job.geo_precision.is_(None), Job.lat.is_not(None), Job.lon.is_not(None))


def _at_town_centre(job: Job) -> bool:
    centre = lookup_city(job.city)
    return (centre is not None and abs(job.lat - centre[0]) < SAME_SPOT
            and abs(job.lon - centre[1]) < SAME_SPOT)


def recheck_locations(
    session: Session,
    geocoder: Geocoder,
    profile: Profile,
    places: list[Place],
    streets=None,
    now: datetime | None = None,
    limit: int = BATCH,
) -> int:
    """Check up to `limit` jobs that were stored without a precision. Returns how many."""
    now = now or utcnow()
    since = now - timedelta(days=RESCORE_WITHIN_DAYS)
    jobs = session.scalars(_pending(Job).order_by(Job.id.desc()).limit(limit)).all()
    found = rescored = 0
    for job in jobs:
        job.geo_precision = "city" if _at_town_centre(job) else "address"
        if job.geo_precision == "city":
            names = place_names(job.title)
            geo = geocoder.resolve(job.city, None, names) if names else None
            if geo is not None and geo.precision == "address":
                job.lat, job.lon, job.geo_precision = geo.lat, geo.lon, "address"
                found += 1
        first_seen = job.first_seen_at
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=UTC)  # SQLite hands back naive UTC
        if first_seen < since or job.duplicate_of is not None:
            continue
        was_set_aside = job.status == JobStatus.IGNORED.value and job.score == 0
        if rescore_job(job, profile, places, now, streets):
            rescored += 1
        if was_set_aside and job.status == JobStatus.NEW.value and job.alerted_at is None:
            job.alerted_at = now  # back among the open ads, but no alert for it
    if jobs:
        session.add(Event(kind="locations_rechecked",
                          data={"checked": len(jobs), "found": found, "rescored": rescored}))
        session.flush()
        log.info("checked the location of %d jobs: %d exact places found, %d scored again",
                 len(jobs), found, rescored)
    return len(jobs)


def make_locator(session_factory: sessionmaker[Session],
                 geocoder_factory: Callable[[Session], Geocoder] = Geocoder) -> Callable[[], int]:
    """What the scheduler calls: one batch with its own database session."""

    def run() -> int:
        with session_factory() as session:
            if not session.scalar(_pending().limit(1)):
                return 0  # nothing to check: do not even open a connection to the map service
            profile = get_or_create_profile(session)
            places = list(session.scalars(select(Place).where(Place.active)))
            with geocoder_factory(session) as geocoder:
                count = recheck_locations(session, geocoder, profile, places, get_street_map())
            session.commit()
            return count

    return run
