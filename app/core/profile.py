"""Profile service: load/create the single profile and pick which CV to send for a job."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Cv, Profile


@dataclass(frozen=True)
class JobFacts:
    """What CV selection needs to know about a job."""

    country: str = "HR"
    category: str | None = None
    text: str = ""  # title + description


def get_or_create_profile(session: Session) -> Profile:
    profile = session.scalar(select(Profile).order_by(Profile.id).limit(1))
    if profile is None:
        profile = Profile()
        session.add(profile)
        session.flush()
    return profile


def _rule_specificity(rule: dict, facts: JobFacts) -> int | None:
    """None = rule does not match. Otherwise higher = more specific.

    Rule keys (all optional, all given keys must match):
      country: "HR"            job country code
      not_country: "HR"        any country except this one
      categories: [...]        job category must be one of these
      keywords_any: [...]      any keyword appears in title/description (case-insensitive)
    """
    score = 0
    if "country" in rule:
        if rule["country"].upper() != facts.country.upper():
            return None
        score += 1
    if "not_country" in rule:
        if rule["not_country"].upper() == facts.country.upper():
            return None
        score += 1
    if rule.get("categories"):
        if facts.category not in rule["categories"]:
            return None
        score += 1
    if rule.get("keywords_any"):
        text = facts.text.lower()
        if not any(k.lower() in text for k in rule["keywords_any"]):
            return None
        score += 2  # a keyword hit is the strongest signal (e.g. tech job -> English CV)
    return score


def select_cv(profile: Profile, facts: JobFacts) -> Cv | None:
    """Most specific matching CV; ties go to the default CV; then default; then the first."""
    if not profile.cvs:
        return None
    best: tuple[int, bool, Cv] | None = None
    for cv in profile.cvs:
        spec = _rule_specificity(cv.rule or {}, facts)
        if spec is None:
            continue
        cand = (spec, cv.is_default, cv)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is not None:
        return best[2]
    return next((c for c in profile.cvs if c.is_default), profile.cvs[0])
