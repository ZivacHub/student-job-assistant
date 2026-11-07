"""Find the same ad posted on several sites (or reposted), so we alert/apply only once."""

from datetime import timedelta

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.geo.cities import normalize
from app.models import Job
from app.models.base import utcnow

TITLE_MATCH = 88  # rapidfuzz ratio 0-100 on cleaned titles
LOOSE_TITLE_MATCH = 60  # enough if the descriptions also agree
DESCRIPTION_MATCH = 80
MIN_DESCRIPTION_CHARS = 80
EMPLOYER_MATCH = 85
TITLE_ONLY_MATCH = 93  # stricter when the employer is unknown on one side


# gender endings ("Konobar/ica") and company-form noise ("d.o.o.", "obrt") never identify an ad
_NOISE = {"d", "o", "j", "m", "z", "f", "ica", "ca", "ka", "doo", "obrt", "sd"}


def clean(text: str | None) -> str:
    tokens = normalize((text or "").replace("/", " ")).split()
    return " ".join(t for t in tokens if t not in _NOISE)


def dedupe_key(title: str, employer: str | None, city: str | None) -> str:
    """Stable normalized key; equal keys are certainly the same ad."""
    return "|".join(clean(x) for x in (title, employer, city))


def _same_city(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True  # unknown city: do not block a match
    na, nb = normalize(a), normalize(b)
    return na == nb or na in nb or nb in na


def _title_core(title: str, employer: str | None) -> str:
    """Cleaned title minus the employer's own words ('Konobar u kafiću Luna' by Luna)."""
    emp = set(clean(employer).split())
    return " ".join(t for t in clean(title).split() if t not in emp)


def _description_alike(a: str | None, b: str | None) -> bool:
    """Real ad text agrees. Short texts are boilerplate ('send your CV') and prove nothing."""
    ca, cb = clean(a), clean(b)
    if len(ca) < MIN_DESCRIPTION_CHARS or len(cb) < MIN_DESCRIPTION_CHARS:
        return False
    return fuzz.ratio(ca[:300], cb[:300]) >= DESCRIPTION_MATCH


def find_duplicate(
    session: Session,
    title: str,
    employer: str | None,
    city: str | None,
    within_days: int = 14,
    description: str | None = None,
) -> Job | None:
    """An existing recent job that is the same ad, else None.

    Same employer + same town is NOT enough: 'Konobar' and 'Konobar - vikend smjena, noćna' are
    two different openings. Titles must be (almost) equal once gender endings, company forms and
    the employer's own name are stripped, or the descriptions must be alike.
    """
    key = dedupe_key(title, employer, city)
    since = utcnow() - timedelta(days=within_days)
    candidates = session.scalars(
        select(Job).where(Job.first_seen_at >= since, Job.duplicate_of.is_(None))
    ).all()
    for c in candidates:
        if c.dedupe_key == key:
            return c
    ne = clean(employer)
    nt = _title_core(title, employer)
    for c in candidates:
        if not _same_city(city, c.city):
            continue
        title_ratio = fuzz.ratio(nt, _title_core(c.title, c.employer))
        if ne and c.employer:
            same_employer = fuzz.token_set_ratio(ne, clean(c.employer)) >= EMPLOYER_MATCH
            loose = title_ratio >= LOOSE_TITLE_MATCH and _description_alike(
                description, c.description
            )
            if same_employer and (title_ratio >= TITLE_MATCH or loose):
                return c
        elif title_ratio >= TITLE_ONLY_MATCH and city and c.city:
            return c
    return None
