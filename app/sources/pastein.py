"""Paste-in: turn text copied from Facebook, WhatsApp, Njuškalo or a friend's tip into a Job.

Pasted text is untrusted data: we only read fields out of it. Nothing is fetched from the link
Ivan may paste along (Facebook and Njuškalo must not be scraped), so the URL is only stored.
Every guessed field can be overridden: the Phase 4 form shows the guess and lets Ivan fix it.
"""

import hashlib
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.geo.cities import find_city_in_text
from app.geo.nominatim import Geocoder
from app.models import Job, Place, Profile
from app.scoring.parse import parse_pay
from app.sources.base import BaseSource, ListingItem, RawJob
from app.sources.ingest import get_or_create_source, ingest_raw

MAX_CHARS = 20_000
SOURCE_KEY = "paste"

_LEADING_NOISE = re.compile(r"^[\W_]+", re.UNICODE)
_COMPANY = re.compile(
    r"((?:[A-ZČĆŽŠĐ0-9][\wČĆŽŠĐčćžšđ&\-]*\s+){0,4}(?:j\.d\.o\.o\.|d\.o\.o\.|d\.d\.|obrt))",
)


class PasteIn(BaseSource):
    """Registers the 'paste' source row. It never polls: list_jobs is always empty."""

    key = SOURCE_KEY
    name = "Pasted ads"
    interval_minutes = 0  # 0 = not scheduled

    def list_jobs(self, max_pages: int = 1) -> list[ListingItem]:
        return []

    def fetch_job(self, item: ListingItem) -> RawJob:  # pragma: no cover
        raise NotImplementedError("pasted ads are not fetched")


def _guess_title(text: str) -> str:
    """First real line; if it is a long paragraph, its first sentence."""
    for line in text.splitlines():
        line = _LEADING_NOISE.sub("", line).strip()
        if len(line) < 3:
            continue
        if len(line) <= 120:
            return line
        first_sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
        if 3 <= len(first_sentence) <= 120:
            return first_sentence
        return line[:100].rsplit(" ", 1)[0] + "…"
    return "Pasted ad"


def _guess_employer(text: str) -> str | None:
    m = _COMPANY.search(text)
    return " ".join(m.group(1).split()) if m else None


def parse_pasted_ad(
    text: str,
    url: str | None = None,
    *,
    title: str | None = None,
    employer: str | None = None,
    city: str | None = None,
    pay_eur_hour: float | None = None,
    posted_at: datetime | None = None,
) -> RawJob:
    """Best-guess RawJob from free text. Any keyword argument overrides the guess."""
    text = (text or "").strip()[:MAX_CHARS]
    if not text:
        raise ValueError("nothing to parse: the pasted text is empty")
    digest = hashlib.sha1(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:16]
    pay_text = None
    guessed_pay = parse_pay(text)
    if guessed_pay is not None:
        pay_text = f"{guessed_pay:.2f} €"
    return RawJob(
        external_id=digest,
        url=url or f"paste:{digest}",
        title=title or _guess_title(text),
        employer=employer or _guess_employer(text),
        city=city or find_city_in_text(text),
        description=text,
        pay_text=pay_text,
        pay_eur_hour=pay_eur_hour if pay_eur_hour is not None else guessed_pay,
        posted_at=posted_at,
        contact_text=text,
        extra={"pasted": True},
    )


def ingest_pasted(
    session: Session,
    profile: Profile,
    places: list[Place],
    geocoder: Geocoder,
    text: str,
    url: str | None = None,
    now: datetime | None = None,
    **overrides,
) -> tuple[Job, bool]:
    """Store a pasted ad. Returns (job, created); the same text pasted twice gives the first job."""
    source = get_or_create_source(session, PasteIn())
    raw = parse_pasted_ad(text, url, **overrides)
    job = ingest_raw(session, source, raw, profile, places, geocoder, baseline=False, now=now)
    if job is None:
        existing = (
            session.query(Job).filter_by(source_id=source.id, external_id=raw.external_id).one()
        )
        return existing, False
    session.commit()
    return job, True
