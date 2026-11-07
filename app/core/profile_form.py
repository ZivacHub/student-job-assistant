"""Reading and saving the profile form (name, transport, job types, alert rules, CV rules)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from app.notify.policy import parse_hhmm
from app.scoring.parse import CATEGORY_HR


class FormLike(Protocol):
    def get(self, key: str, default=None): ...
    def getlist(self, key: str) -> list: ...


TONES = {"Vi": "Vi (službeno)", "ti": "ti"}
MAX_PAY = 40.0
CV_COUNTRIES = {"": "Svi oglasi", "HR": "Samo oglasi u Hrvatskoj"}


@dataclass
class ProfileForm:
    full_name: str = ""
    tone: str = "Vi"
    has_car: bool = False
    wanted: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    min_pay: float | None = None
    availability: str = ""
    signature: str = ""
    notify_threshold: int = 60
    digest_threshold: int = 40
    digest_interval_hours: int = 3
    quiet_start: str | None = None
    quiet_end: str | None = None
    default_cv: int | None = None
    cv_rules: dict[int, dict] = field(default_factory=dict)


def _int(form: Mapping, key: str, lo: int, hi: int, default: int, label: str,
         errors: dict[str, str]) -> int:
    raw = (form.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors[key] = f"{label}: upišite cijeli broj."
        return default
    if not lo <= value <= hi:
        errors[key] = f"{label}: broj mora biti između {lo} i {hi}."
    return value


def _signature(raw: str | None) -> str:
    """The sign-off under a message: up to 4 short lines (name, phone ...), no blank lines."""
    lines = [" ".join(line.split()) for line in (raw or "").splitlines() if line.strip()]
    return "\n".join(lines[:4])[:300]


def parse_profile_form(form: FormLike, cv_ids: list[int]) -> tuple[ProfileForm, dict[str, str]]:
    errors: dict[str, str] = {}
    out = ProfileForm(
        full_name=" ".join((form.get("full_name") or "").split())[:120],
        tone=form.get("tone") if form.get("tone") in TONES else "Vi",
        has_car=form.get("has_car") in ("on", "1", "true"),
        availability=(form.get("availability") or "").strip()[:500],
        signature=_signature(form.get("signature")),
    )
    out.wanted = [c for c in form.getlist("wanted") if c in CATEGORY_HR]
    out.excluded = [c for c in form.getlist("excluded") if c in CATEGORY_HR]
    both = sorted(set(out.wanted) & set(out.excluded))
    if both:
        names = ", ".join(CATEGORY_HR[c] for c in both)
        errors["wanted"] = f"Ista vrsta posla ne može biti i željena i neželjena: {names}."

    raw_pay = (form.get("min_pay") or "").strip().replace(",", ".")
    if raw_pay:
        try:
            out.min_pay = float(raw_pay)
            if not 0 <= out.min_pay <= MAX_PAY:
                errors["min_pay"] = f"Najmanja satnica mora biti između 0 i {MAX_PAY:.0f} €."
        except ValueError:
            errors["min_pay"] = "Najmanja satnica mora biti broj, npr. 6.50."

    out.notify_threshold = _int(form, "notify_threshold", 0, 100, 60, "Hitna obavijest", errors)
    out.digest_threshold = _int(form, "digest_threshold", 0, 100, 40, "Skupna obavijest", errors)
    out.digest_interval_hours = _int(form, "digest_interval_hours", 1, 48, 3,
                                     "Razmak skupnih obavijesti", errors)
    if "digest_threshold" not in errors and out.digest_threshold > out.notify_threshold:
        errors["digest_threshold"] = ("Granica za skupnu obavijest ne smije biti viša od granice "
                                      "za hitnu obavijest.")

    start, end = (form.get("quiet_start") or "").strip(), (form.get("quiet_end") or "").strip()
    if bool(start) != bool(end):
        errors["quiet_start"] = "Upišite i početak i kraj tihih sati, ili oboje ostavite praznima."
    elif start:
        try:
            parse_hhmm(start)
            parse_hhmm(end)
            out.quiet_start, out.quiet_end = start, end
        except ValueError:
            errors["quiet_start"] = "Tihi sati moraju biti u obliku 22:00."

    chosen = (form.get("cv_default") or "").strip()
    if cv_ids:
        out.default_cv = int(chosen) if chosen.isdigit() and int(chosen) in cv_ids else cv_ids[0]
    for cv_id in cv_ids:
        rule: dict = {}
        country = (form.get(f"cv_{cv_id}_country") or "").strip()
        if country in CV_COUNTRIES and country:
            rule["country"] = country
        words = [w.strip() for w in (form.get(f"cv_{cv_id}_keywords") or "").split(",")]
        words = [w for w in words if w][:30]
        if words:
            rule["keywords_any"] = words
        out.cv_rules[cv_id] = rule
    return out, errors


def save_profile(session: Session, profile, data: ProfileForm) -> None:
    profile.full_name = data.full_name or None
    profile.tone = data.tone
    profile.has_car = data.has_car
    profile.categories_wanted = list(data.wanted)
    profile.categories_excluded = list(data.excluded)
    profile.min_pay_eur_hour = data.min_pay
    profile.availability = data.availability or None
    profile.signature = data.signature or None
    profile.notify_threshold = data.notify_threshold
    profile.digest_threshold = data.digest_threshold
    profile.digest_interval_hours = data.digest_interval_hours
    profile.quiet_start, profile.quiet_end = data.quiet_start, data.quiet_end
    for cv in profile.cvs:
        cv.is_default = cv.id == data.default_cv
        cv.rule = data.cv_rules.get(cv.id, cv.rule or {})
    session.flush()
