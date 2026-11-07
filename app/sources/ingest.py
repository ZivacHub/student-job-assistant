"""Turn RawJobs from any source into scored Job rows (dedupe, geo, contacts, score, audit log)."""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.apply.service import ensure_draft
from app.core.settings import get_settings
from app.extract.contacts import choose_channel, extract_contacts
from app.geo.cities import lookup_city
from app.geo.mapstore import get_street_map
from app.geo.nominatim import Geocoder
from app.geo.placenames import place_names
from app.geo.travel import haversine_km
from app.models import Contact, Event, Job, JobStatus, Place, Profile, Source
from app.models.base import utcnow
from app.scoring.dedupe import dedupe_key, find_duplicate
from app.scoring.engine import score_job
from app.scoring.parse import detect_category, parse_pay
from app.sources.base import BaseSource, ListingItem, RawJob
from app.sources.http import (
    RobotsDisallowed,
    SourceBlocked,
    SourceError,
    SourceNotFound,
    SourceParseError,
)

log = logging.getLogger(__name__)

# the site's own category emoji -> our category (used only when the title gives no clue)
LABEL_CATEGORY = {
    "☕": "ugostiteljstvo",
    "💪": "fizicki",
    "🎧": "call_centar",
    "🛒": "trgovina",
    "🏫": "instrukcije",
    "🥳": "djeca",
}


def _with_note(description: str, note: str | None) -> str:
    """The employer's own instruction on how to apply stays part of the saved ad text."""
    if note and note not in description:
        return f"{description}\n\nNapomena poslodavca: {note}".strip()
    return description


def pick_city(cities: str | None, places: list[Place]) -> str | None:
    """'Zagreb, Osijek, Đakovo' -> the town closest to any of our places."""
    if not cities:
        return None
    options = [c.strip() for c in cities.split(",") if c.strip()]
    if len(options) <= 1:
        return options[0] if options else None
    anchors = [(p.lat, p.lon) for p in places if p.active and p.lat is not None]
    if not anchors:
        return options[0]

    def nearest(city: str) -> float:
        coords = lookup_city(city)
        if not coords:
            return float("inf")
        return min(haversine_km(a, b, *coords) for a, b in anchors)

    return min(options, key=nearest)


def get_or_create_source(session: Session, src: BaseSource) -> Source:
    row = session.scalar(select(Source).where(Source.key == src.key))
    if row is None:
        row = Source(key=src.key, name=src.name, interval_minutes=src.interval_minutes)
        session.add(row)
        session.commit()  # durable now: a later rollback for one bad ad must not remove it
    return row


def ingest_raw(
    session: Session,
    source: Source,
    raw: RawJob,
    profile: Profile,
    places: list[Place],
    geocoder: Geocoder,
    own_domains: tuple[str, ...] = (),
    baseline: bool = False,
    now: datetime | None = None,
) -> Job | None:
    """Store one job. Returns the new Job, or None if we already had it."""
    exists = session.scalar(
        select(Job.id).where(Job.source_id == source.id, Job.external_id == raw.external_id)
    )
    if exists:
        return None
    now = now or utcnow()

    chosen_city = pick_city(raw.city, places)
    job = Job(
        source_id=source.id,
        external_id=raw.external_id,
        url=raw.url,
        title=raw.title,
        employer=raw.employer,
        description=_with_note(raw.description, raw.apply_note),
        city=chosen_city,
        address=raw.address,
        pay_text=raw.pay_text,
        posted_at=raw.posted_at,
        expires_at=raw.expires_at,
        first_seen_at=now,
        dedupe_key=dedupe_key(raw.title, raw.employer, chosen_city),
    )
    job.pay_eur_hour = raw.pay_eur_hour if raw.pay_eur_hour is not None else parse_pay(raw.pay_text)

    # check for duplicates BEFORE adding the job, or it would match itself
    dup = find_duplicate(
        session, raw.title, raw.employer, chosen_city, description=raw.description
    )
    session.add(job)
    if dup is not None:
        job.duplicate_of = dup.id
        job.status = JobStatus.SEEN.value
        session.flush()
        session.add(Event(kind="duplicate", job_id=job.id, data={"of": dup.id}))
        return job

    job.category = (
        detect_category(raw.title)
        or LABEL_CATEGORY.get(raw.site_label or "")
        or detect_category(raw.description[:400])
    )
    geo = geocoder.resolve(chosen_city, raw.address, place_names(raw.title))
    if geo:
        job.lat, job.lon, job.geo_precision = geo.lat, geo.lon, geo.precision

    text = "\n".join(x for x in (raw.contact_text, raw.description) if x)
    contacts = extract_contacts(text, ignore_domains=own_domains, own_url=raw.url)
    for c in contacts:
        job.contacts.append(Contact(kind=c.kind, value=c.value, confidence=c.confidence))
    # "site" only when the ad has a real web page to apply on (a pasted ad without a link has none)
    has_page = raw.url.startswith(("http://", "https://"))
    job.channel = choose_channel(contacts, has_apply_url=has_page, note=raw.apply_note)

    result = score_job(job, profile, places, now, get_street_map())
    job.score, job.score_reasons = result.score, result.reasons
    if result.excluded:
        job.status = JobStatus.IGNORED.value
    if baseline:  # first run of a source: do not treat the existing backlog as new
        job.status = JobStatus.SEEN.value if not result.excluded else job.status
        job.alerted_at = now
    session.flush()
    session.add(
        Event(kind="collected", job_id=job.id,
              data={"score": result.score, "excluded": result.excluded, "channel": job.channel})
    )
    if not baseline and not result.excluded:
        _prepare_draft(session, job, profile, now)
    return job


def _prepare_draft(session: Session, job: Job, profile: Profile, now: datetime) -> None:
    """A good match with an email address in the ad gets a draft that waits for Ivan's approval
    (plan 5.1, D33). Nothing is sent, and a problem here must never stop the collector."""
    try:
        ensure_draft(session, job, profile, now, dry_run=get_settings().dry_run)
    except Exception:
        log.exception("could not prepare a draft for job %s", job.id)


MAX_BLOCKED_BEFORE_DISABLE = 3


def _finish_run(session: Session, source: Source, status: str, error: str | None,
                items: int, baseline_done: bool) -> None:
    """Record how a run ended. Only a clean run ('ok') completes the baseline."""
    source.last_run_at = utcnow()
    source.last_status, source.last_error, source.last_items = status, error, items
    if status == "ok":
        source.consecutive_failures = 0
        if baseline_done and source.baselined_at is None:
            source.baselined_at = utcnow()
    else:
        source.consecutive_failures += 1
        if status == "blocked" and source.consecutive_failures >= MAX_BLOCKED_BEFORE_DISABLE:
            source.enabled = False  # the site keeps saying no: stop, never evade
            log.error("%s disabled after %d blocked runs", source.key, source.consecutive_failures)
    session.add(Event(kind="source_run", data={"source": source.key, "status": status,
                                               "error": error, "new": items}))
    session.commit()


def collect(
    session: Session,
    src: BaseSource,
    profile: Profile,
    places: list[Place],
    geocoder: Geocoder,
    max_pages: int = 1,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[Job]:
    """One polling run for a source: list, fetch details only for unknown ads, ingest.

    Never raises for source problems: they are recorded on the Source row (status, error,
    consecutive_failures) so the scheduler and the Sources page can show them.
    """
    source = get_or_create_source(session, src)
    baseline = source.baselined_at is None  # backlog until one clean run has finished
    known = set(session.scalars(select(Job.external_id).where(Job.source_id == source.id)))
    created: list[Job] = []

    try:
        listing = src.list_jobs(max_pages)
    except SourceParseError as e:
        _finish_run(session, source, "parse_error", str(e), 0, baseline)
        return created
    except (SourceBlocked, RobotsDisallowed) as e:
        _finish_run(session, source, "blocked", str(e), 0, baseline)
        return created
    except SourceError as e:
        _finish_run(session, source, "error", str(e), 0, baseline)
        return created
    except Exception as e:  # a bug in a parser must show up on the Sources page, not vanish
        log.exception("%s: listing failed unexpectedly", src.key)
        _finish_run(session, source, "error", f"{type(e).__name__}: {e}", 0, baseline)
        return created

    new_items: list[ListingItem] = [i for i in listing if i.external_id not in known]
    if limit is not None:
        new_items = new_items[:limit]

    errors: list[str] = []
    status = "ok"
    for item in new_items:
        try:
            raw = src.fetch_job(item)
            job = ingest_raw(
                session, source, raw, profile, places, geocoder,
                own_domains=src.own_domains, baseline=baseline, now=now,
            )
            session.commit()
        except SourceNotFound:
            continue  # the ad was removed between listing and detail: not a failure
        except (SourceBlocked, RobotsDisallowed) as e:
            session.rollback()
            errors.append(str(e))
            status = "blocked"
            break  # do not keep knocking
        except SourceError as e:
            session.rollback()
            errors.append(f"{item.external_id}: {e}")
            status = "partial"
            continue
        except Exception as e:  # a bug on one ad must not hide the others
            session.rollback()
            log.exception("%s: ingest failed for %s", src.key, item.external_id)
            errors.append(f"{item.external_id}: {type(e).__name__}: {e}")
            status = "partial"
            continue
        if job is not None:
            created.append(job)

    _finish_run(session, source, status, "; ".join(errors)[:1000] or None, len(created), baseline)
    log.info("%s: %d new, status=%s (baseline=%s)", src.key, len(created), status, baseline)
    return created
