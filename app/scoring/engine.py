"""Score a job 0-100 against the profile. Every point comes with a human-readable reason.

Hard filters first (excluded category, pay below minimum, outside every travel limit), then
points: category, travel time, pay, freshness (newer wins), how we can apply, plus bonus points
for being near a "bonus place" (e.g. a partner's address). Weights are editable in the profile.
"""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from app.geo.travel import INF, Access, accesses, haversine_km

TOWN_UNCERTAINTY_KM = 5.0  # the town table is good to a few km; Osijek is about 5 km across
# Share of the travel points an ad gets when only its town is known (no minutes are claimed):
# "probably near", since the town is small. Ivan chose 80 % on 2026-10-26 (D40) after seeing that
# the "middle" 40 % (what an ad with no location gets) took 24 of his 26 alert-level ads under the
# alert line.
TOWN_ONLY_TRAVEL_SHARE = 0.8
DEFAULT_WEIGHTS = {"category": 25, "travel": 30, "pay": 15, "freshness": 20, "channel": 10}
BONUS_CAP = 15
FRESHNESS_HALF_LIFE_H = 6.0  # an ad loses half its freshness points every 6 hours
DEFAULT_PAY_FLOOR = 5.0
PAY_SPAN = 3.0  # EUR above the floor that earns full pay points
CHANNEL_FACTOR = {"email": 1.0, "phone": 0.7, "site": 0.5, "none": 0.0}


@dataclass
class ScoreResult:
    score: int
    excluded: bool = False
    # {"k": kind, "pts": n, "text": English sentence, "data": facts for other languages}
    reasons: list[dict] = field(default_factory=list)

    def add(self, kind: str, pts: float, text: str, **data) -> None:
        self.reasons.append({"k": kind, "pts": round(pts, 1), "text": text, "data": data})


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)  # SQLite returns naive datetimes; we store UTC
    return dt


def _excluded(reason_kind: str, text: str) -> ScoreResult:
    res = ScoreResult(score=0, excluded=True)
    res.add(reason_kind, 0, text)
    return res


def _town_only(accs: list[Access], job, places: list) -> tuple[list[Access], list[str]]:
    """A job whose ad names only the town sits at the town centre, which is no more than a guess
    for a place inside the town (job "TC Portanova": 200 m from home on the map, an hour on foot
    in real life). Ways from such a place are dropped and its label is returned instead, so no
    minutes are claimed. Ways from places farther away stay, as rough estimates (`approx`)."""
    offered = {a.place_label for a in accs}
    near = [
        p.label for p in places
        if p.active and p.lat is not None and p.lon is not None and p.label in offered
        and haversine_km(p.lat, p.lon, job.lat, job.lon) <= TOWN_UNCERTAINTY_KM
    ]
    kept = [replace(a, approx=True) for a in accs if a.place_label not in near]
    return kept, near


def score_job(job, profile, places: list, now: datetime | None = None,
              streets=None) -> ScoreResult:
    """job: Job-like, profile: Profile-like, places: Place-like list. Pure function.

    `streets` is the street map (see `app.geo.streets`); without it travel times are the rough
    straight-line estimate and every reason says so (`approx`).
    """
    now = _aware(now) or datetime.now(UTC)
    w = {**DEFAULT_WEIGHTS, **(profile.weights or {})}
    wanted = list(profile.categories_wanted or [])
    excluded_cats = list(profile.categories_excluded or [])
    has_car = bool(profile.has_car)

    # ---- hard filters
    if job.category and job.category in excluded_cats:
        return _excluded("category", f"Category '{job.category}' is on your exclude list")
    min_pay = profile.min_pay_eur_hour
    if job.pay_eur_hour is not None and min_pay and job.pay_eur_hour < min_pay:
        return _excluded(
            "pay", f"Pay {job.pay_eur_hour:.2f} €/h is below your minimum {min_pay:.2f}"
        )

    accs: list[Access] = []
    only_town: list[str] = []  # places whose time to a town-centre-only ad cannot be told
    if job.lat is not None and job.lon is not None:
        accs = accesses(job.lat, job.lon, places, has_car, streets)
        if getattr(job, "geo_precision", None) == "city":
            accs, only_town = _town_only(accs, job, places)
    reachable = [a for a in accs if a.reachable]
    if accs and not reachable and not only_town:
        finite = [a for a in accs if a.minutes != INF]
        if not finite:
            return _excluded("travel", "No way to get there by any of your ways of travelling")
        best = min(finite, key=lambda a: a.minutes / a.max_minutes)
        return _excluded(
            "travel",
            f"Too far: {best.minutes:.0f} min by {best.mode} from {best.place_label} "
            f"(limit {best.max_minutes})",
        )

    res = ScoreResult(score=0)

    # ---- category
    if not wanted:
        pts, why = 0.6, "No wanted categories set"
    elif job.category in wanted:
        pts, why = 1.0, f"Wanted category: {job.category}"
    elif job.category is None:
        pts, why = 0.4, "Category unknown"
    else:
        pts, why = 0.2, f"'{job.category}' is not in your wanted list"
    res.add("category", w["category"] * pts, why, category=job.category,
            wanted=job.category in wanted if wanted else None)

    # ---- travel
    if reachable:
        best = min(reachable, key=lambda a: a.minutes / a.max_minutes)
        ratio = best.minutes / best.max_minutes
        res.add(
            "travel",
            w["travel"] * (1 - 0.8 * ratio),
            f"{best.minutes:.0f} min by {best.mode} from {best.place_label}"
            + (" (rough estimate)" if best.approx else ""),
            minutes=round(best.minutes), mode=best.mode, place=best.place_label,
            approx=best.approx,
        )
        bonus_places: dict[str, int] = {}
        for a in reachable:
            if a.bonus_points > 0:
                bonus_places[a.place_label] = a.bonus_points
        bonus = min(BONUS_CAP, sum(bonus_places.values()))
        for label in bonus_places:
            res.add("bonus", 0, f"Near {label}", place=label)
        if bonus:
            names = ", ".join(bonus_places)
            res.add("bonus", bonus, f"Bonus points for being reachable from {names}")
    elif only_town:
        res.add("travel", w["travel"] * TOWN_ONLY_TRAVEL_SHARE,
                f"Ad names only the town, so the time from {only_town[0]} cannot be told",
                unknown=True, city_only=True, place=only_town[0])
    else:
        res.add("travel", w["travel"] * 0.4, "Location or places unknown, travel not checked",
                unknown=True)

    # ---- pay
    if job.pay_eur_hour is None:
        res.add("pay", w["pay"] * 0.4, "Pay not stated", eur=None)
    else:
        low = min_pay or DEFAULT_PAY_FLOOR
        ratio = _clamp((job.pay_eur_hour - low) / PAY_SPAN)
        res.add("pay", w["pay"] * (0.3 + 0.7 * ratio), f"{job.pay_eur_hour:.2f} €/h",
                eur=job.pay_eur_hour)

    # ---- freshness
    published = _aware(job.posted_at) or _aware(job.first_seen_at) or now
    age_h = max(0.0, (now - published).total_seconds() / 3600)
    res.add("freshness", w["freshness"] * 0.5 ** (age_h / FRESHNESS_HALF_LIFE_H),
            f"Posted {age_h * 60:.0f} min ago" if age_h < 2 else f"Posted {age_h:.1f} h ago",
            age_minutes=round(age_h * 60))

    # ---- how we can apply
    channel = job.channel or "none"
    res.add("channel", w["channel"] * CHANNEL_FACTOR.get(channel, 0.0), f"Apply by: {channel}",
            channel=channel)

    res.score = min(100, round(sum(r["pts"] for r in res.reasons)))
    return res
