"""Inbox (plan 4.1): which stored ads to show, in what order, and how each row reads.

Plain logic with no web code, so it is easy to test. Everything that comes from an ad is
untrusted text: the template escapes it, and the only thing taken from an ad as a link is its
address, accepted only when it is http(s).

Words used on screen (Croatian) and what they mean:
- "Otvoreni"  = ads Ivan can still act on (status new or seen)
- "Samo nove" = ads not looked at yet (status new)
- "Svi oglasi" = everything, including ignored, snoozed and applied ones
An ad the system set aside (status ignored and score 0: excluded category, pay below the
minimum, too far from every place) is only shown under "Svi oglasi", with the reason.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from math import ceil
from urllib.parse import urlencode, urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, joinedload

from app.core.places import MODE_LABELS_LOWER
from app.models import Job, JobStatus, Profile
from app.models.base import utcnow

PAGE_SIZE = 50
OPEN_STATUSES = (JobStatus.NEW.value, JobStatus.SEEN.value)
MAX_PAY_TEXT = 40
DISPLAY_NAMES = {"paste": "Ručno dodani oglasi"}  # sources whose stored name is not Croatian


def source_name(source) -> str:
    return DISPLAY_NAMES.get(source.key, source.name) if source else ""

VIEW_LABELS = {"otvoreni": "Otvoreni", "nove": "Samo nove", "sve": "Svi oglasi"}
CHANNEL_LABELS = {"email": "E-mail", "phone": "Telefon", "site": "Stranica", "none": "Bez kontakta"}
SCORE_STEPS = (0, 40, 60, 80)
STATUS_LABELS = {
    JobStatus.NEW.value: "Novo",
    JobStatus.APPLIED.value: "Prijavljeno",
    JobStatus.IGNORED.value: "Zanemareno",
    JobStatus.SNOOZED.value: "Odgođeno",
}
# why the system set an ad aside, from the kind of the (only) score reason it stored
SET_ASIDE_REASONS = {
    "category": "vrsta posla je na popisu koji ne želite",
    "pay": "plaća je ispod vašeg minimuma",
    "travel": "predaleko od svih vaših mjesta",
}
DEFAULT_SET_ASIDE = "ne odgovara vašem profilu"


def count_phrase(n: int) -> str:
    """'1 oglas', '5 oglasa'."""
    return f"{n} {'oglas' if n % 10 == 1 and n % 100 != 11 else 'oglasa'}"


# ---------------------------------------------------------------- filters
@dataclass(frozen=True)
class InboxFilters:
    view: str = "otvoreni"
    channel: str = ""  # "" = every way of applying
    min_score: int = 0
    page: int = 1

    @property
    def is_default(self) -> bool:
        return (self.view, self.channel, self.min_score) == ("otvoreni", "", 0)

    def query(self, **changes) -> str:
        """The address of this view with some filters changed ('' when nothing differs)."""
        f = replace(self, **changes)
        params = {}
        if f.view != "otvoreni":
            params["prikaz"] = f.view
        if f.channel:
            params["kanal"] = f.channel
        if f.min_score:
            params["ocjena"] = f.min_score
        if f.page > 1:
            params["stranica"] = f.page
        return "?" + urlencode(params) if params else ""


def _int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        number = int((value or "").strip())
    except ValueError:
        return default
    return max(low, min(high, number))


def parse_filters(params: Mapping[str, str]) -> InboxFilters:
    """Read the address parameters. Anything unknown or broken falls back to the default."""
    view = params.get("prikaz", "")
    channel = params.get("kanal", "")
    return InboxFilters(
        view=view if view in VIEW_LABELS else "otvoreni",
        channel=channel if channel in CHANNEL_LABELS else "",
        min_score=_int(params.get("ocjena"), 0, 0, 100),
        page=_int(params.get("stranica"), 1, 1, 100000),
    )


# ---------------------------------------------------------------- one row
@dataclass
class JobRow:
    id: int
    title: str
    employer: str | None
    city: str | None
    score: int | None
    band: str  # high / mid / low (by the alert thresholds), off (set aside), none (no score yet)
    set_aside: str | None  # Croatian reason when the system set the ad aside
    travel_minutes: int | None
    travel_how: str | None  # 'autom od: Dom'
    travel_unknown: bool
    travel_approx: bool  # a rough straight-line estimate, shown as "oko 12 min"
    pay: str | None
    channel: str
    channel_label: str
    status: str
    status_label: str | None
    age: str | None
    source: str
    url: str | None  # the ad's own page, only when it is http(s)
    travel_city_only: bool = False  # the ad names only the town: no minutes can be told (4.10)


@dataclass
class Inbox:
    filters: InboxFilters
    rows: list[JobRow]
    matching: int  # ads matching all filters, over every page
    total: int  # every stored ad that is not a duplicate
    counts: dict[str, int]  # per view
    page: int
    pages: int
    has_places: bool
    summary: str = field(default="")


def safe_http_url(url: str | None) -> str | None:
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    return text if parts.scheme in ("http", "https") and parts.netloc else None


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def describe_age(then: datetime | None, now: datetime) -> str | None:
    """'prije 5 min', 'prije 3 sata', 'prije 2 dana'."""
    then = _aware(then)
    if then is None:
        return None
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes < 1:
        return "upravo sada"
    if minutes < 60:
        return f"prije {minutes} min"
    hours = minutes // 60
    if hours < 24:
        if hours % 10 == 1 and hours % 100 != 11:
            word = "sat"
        elif 2 <= hours % 10 <= 4 and not 12 <= hours % 100 <= 14:
            word = "sata"
        else:
            word = "sati"
        return f"prije {hours} {word}"
    days = hours // 24
    return f"prije {days} {'dan' if days % 10 == 1 and days % 100 != 11 else 'dana'}"


def format_pay(job: Job) -> str | None:
    if job.pay_eur_hour:
        return f"{job.pay_eur_hour:.2f}".replace(".", ",") + " €/sat"
    text = " ".join((job.pay_text or "").split())
    return text[:MAX_PAY_TEXT] or None


def _travel(reasons: list | None) -> tuple[int | None, str | None, bool, bool]:
    """(minutes, how, unknown, approx) from the stored score reasons."""
    for r in reasons or []:
        if r.get("k") != "travel":
            continue
        data = r.get("data") or {}
        if data.get("unknown"):
            return None, None, True, False
        if data.get("minutes") is not None:
            mode = MODE_LABELS_LOWER.get(data.get("mode") or "", data.get("mode") or "")
            place = data.get("place")
            how = f"{mode} od: {place}" if place else mode
            return int(data["minutes"]), how.strip() or None, False, bool(data.get("approx"))
    return None, None, False, False


def _city_only(reasons: list | None) -> bool:
    """True when the travel time is unknown because the ad names only the town."""
    return any(r.get("k") == "travel" and (r.get("data") or {}).get("city_only")
               for r in reasons or [])


def is_set_aside(job: Job) -> bool:
    """The system filtered it out (see rescore_jobs): ignored with score 0."""
    return job.status == JobStatus.IGNORED.value and job.score == 0


def _set_aside_reason(job: Job) -> str:
    reasons = job.score_reasons or []
    kind = reasons[0].get("k") if len(reasons) == 1 else None
    return SET_ASIDE_REASONS.get(kind or "", DEFAULT_SET_ASIDE)


def score_band(job: Job, notify_threshold: int, digest_threshold: int) -> str:
    if is_set_aside(job):
        return "off"
    if job.score is None:
        return "none"
    if job.score >= notify_threshold:
        return "high"
    if job.score >= digest_threshold:
        return "mid"
    return "low"


def build_row(job: Job, profile: Profile, now: datetime) -> JobRow:
    minutes, how, unknown, approx = _travel(job.score_reasons)
    channel = job.channel if job.channel in CHANNEL_LABELS else "none"
    aside = is_set_aside(job)
    return JobRow(
        id=job.id,
        title=" ".join((job.title or "").split()) or "(bez naslova)",
        employer=" ".join((job.employer or "").split()) or None,
        city=" ".join((job.city or "").split()) or None,
        score=job.score,
        band=score_band(job, profile.notify_threshold, profile.digest_threshold),
        set_aside=_set_aside_reason(job) if aside else None,
        travel_minutes=minutes,
        travel_how=how,
        travel_unknown=unknown,
        travel_approx=approx,
        pay=format_pay(job),
        channel=channel,
        channel_label=CHANNEL_LABELS[channel],
        status=job.status,
        status_label=STATUS_LABELS.get(job.status),
        age=describe_age(job.posted_at or job.first_seen_at, now),
        source=source_name(job.source),
        url=safe_http_url(job.url),
        travel_city_only=_city_only(job.score_reasons),
    )


# ---------------------------------------------------------------- the query
def _conditions(filters: InboxFilters) -> list:
    conditions = [Job.duplicate_of.is_(None)]
    if filters.view == "otvoreni":
        conditions.append(Job.status.in_(OPEN_STATUSES))
    elif filters.view == "nove":
        conditions.append(Job.status == JobStatus.NEW.value)
    if filters.channel:
        conditions.append(Job.channel == filters.channel)
    if filters.min_score:
        conditions.append(Job.score >= filters.min_score)
    return conditions


def _view_counts(session: Session) -> dict[str, int]:
    by_status = dict(
        session.execute(
            select(Job.status, func.count()).where(Job.duplicate_of.is_(None)).group_by(Job.status)
        ).all()
    )
    new = by_status.get(JobStatus.NEW.value, 0)
    seen = by_status.get(JobStatus.SEEN.value, 0)
    return {"otvoreni": new + seen, "nove": new, "sve": sum(by_status.values())}


def _summary(filters: InboxFilters, matching: int) -> str:
    if filters.view == "sve":
        what = f"{count_phrase(matching)} ukupno"
    elif filters.view == "nove":
        what = f"{count_phrase(matching)} koje još niste vidjeli"
    else:
        what = f"{count_phrase(matching)} na koje se još možete javiti"
    return what


def load_inbox(
    session: Session,
    filters: InboxFilters,
    profile: Profile,
    *,
    has_places: bool,
    now: datetime | None = None,
) -> Inbox:
    now = now or utcnow()
    conditions = _conditions(filters)
    matching = session.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
    pages = max(1, ceil(matching / PAGE_SIZE))
    page = min(filters.page, pages)
    jobs = session.scalars(
        select(Job)
        .options(joinedload(Job.source), defer(Job.description))
        .where(*conditions)
        # best score first; ads without a score last; newest first among equals
        .order_by(func.coalesce(Job.score, -1).desc(), Job.first_seen_at.desc(), Job.id.desc())
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    ).all()
    counts = _view_counts(session)
    return Inbox(
        filters=replace(filters, page=page),
        rows=[build_row(job, profile, now) for job in jobs],
        matching=matching,
        total=counts["sve"],
        counts=counts,
        page=page,
        pages=pages,
        has_places=has_places,
        summary=_summary(filters, matching),
    )
