"""Drafts and the approval flow (plan 5.1, 5.2, 5.4b state): who gets a draft, how it is edited,
approved, undone, skipped and confirmed. Every step leaves an Event (the audit log).

Hard rule (Ivan, 2026-10-14, D9): nothing is sent without his approval of that exact draft. An
approval stores a fingerprint of the address, subject, body and CV (`approved_hash`); any change
to the draft afterwards no longer matches it. Sending itself lives in `gate.py`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.apply.texts import (
    MAX_BODY,
    MAX_SUBJECT,
    DraftFacts,
    build_draft_text,
    has_placeholder,
)
from app.core.profile import JobFacts, select_cv
from app.models import (
    Application,
    ApplicationState,
    Contact,
    ContactKind,
    Cv,
    Event,
    Job,
    JobStatus,
    Profile,
)
from app.models.base import utcnow

DRAFT_MAX_AGE = timedelta(days=3)  # an older ad gets no automatic draft
OPEN_JOB_STATUSES = (JobStatus.NEW.value, JobStatus.SEEN.value)
CATCH_UP_LIMIT = 200  # drafts prepared in one go when the queue page opens
CONFIRMED_REMINDER = ("Poslodavac je potvrdio: sada sami napravite online ugovor u Studentskom "
                      "servisu. Aplikacija ništa ne šalje Studentskom servisu.")
UNDO_CHOICES = (1, 30)  # allowed minutes for the undo window
CAP_CHOICES = (1, 50)
COOLDOWN_CHOICES = (0, 90)

_ADDRESS = re.compile(r"^[^@\s,;<>()\[\]\\\"']+@[^@\s,;<>()\[\]\\\"']+\.[^@\s,;<>()\[\]\\\"']+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

STATE_LABELS = {
    ApplicationState.QUEUED.value: "Čeka Vaše odobrenje",
    ApplicationState.DRAFTED.value: "Nacrt",
    ApplicationState.APPROVED.value: "Odobreno",
    ApplicationState.SENDING.value: "Šalje se",
    ApplicationState.SENT.value: "Poslano",
    ApplicationState.REPLIED.value: "Poslodavac je odgovorio",
    ApplicationState.CONFIRMED.value: "Poslodavac je potvrdio",
    ApplicationState.INTERVIEW.value: "Razgovor",
    ApplicationState.REJECTED.value: "Odbijeno",
    ApplicationState.SKIPPED.value: "Preskočeno",
    ApplicationState.FAILED.value: "Slanje nije uspjelo",
}


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


# ---------------------------------------------------------------- who gets a draft
def valid_address(address: str | None) -> bool:
    return bool(address) and bool(_ADDRESS.match(address or ""))


def ad_email(job: Job) -> Contact | None:
    """The email address printed in the ad itself (never one found anywhere else), best first."""
    found = [c for c in job.contacts
             if c.kind == ContactKind.EMAIL.value and c.from_ad and valid_address(c.value)]
    return max(found, key=lambda c: c.confidence, default=None)


def why_no_draft(job: Job, profile: Profile, now: datetime | None = None, *,
                 manual: bool = False) -> str | None:
    """None when a draft may be prepared, else the reason in plain Croatian.

    Automatic rule (D33): an email address in the ad, a score at or above the profile's alert line,
    an ad that is still open, and recent. `manual` (Ivan asked for it on the job page) skips the
    score and the age but never the address or the state of the ad.
    """
    now = now or utcnow()
    if job.duplicate_of is not None:
        return "Oglas je duplikat drugog oglasa."
    if ad_email(job) is None:
        return "U oglasu nema e-adrese poslodavca."
    if job.status not in OPEN_JOB_STATUSES:
        return "Oglas više nije među otvorenima."
    expires = _aware(job.expires_at)
    if expires is not None and expires < now:
        return "Rok prijave za oglas je prošao."
    if manual:
        return None
    if job.score is None or job.score < profile.notify_threshold:
        return f"Nacrt se priprema samo za oglase s ocjenom {profile.notify_threshold} ili više."
    published = _aware(job.posted_at) or _aware(job.first_seen_at)
    if published is not None and now - published > DRAFT_MAX_AGE:
        return "Oglas je stariji od 3 dana."
    return None


# ---------------------------------------------------------------- the draft
def fingerprint(to_email: str | None, subject: str | None, body: str | None,
                cv_id: int | None) -> str:
    """What an approval is bound to: the exact address, subject, text and CV."""
    parts = [to_email or "", subject or "", body or "", "" if cv_id is None else str(cv_id)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def draft_fingerprint(application: Application) -> str:
    return fingerprint(application.to_email, application.subject, application.body,
                       application.cv_id)


def _pick_cv(job: Job, profile: Profile) -> Cv | None:
    text = f"{job.title}\n{job.description or ''}"
    return select_cv(profile, JobFacts(category=job.category, text=text))


def draft_for(job: Job, profile: Profile, cv: Cv | None):
    facts = DraftFacts(
        title=job.title, employer=job.employer, category=job.category,
        source_name=job.source.name if job.source is not None else None,
        availability=profile.availability,
        signature=profile.signature or profile.full_name,
        has_car=bool(profile.has_car), cv_attached=cv is not None,
        cv_language=cv.language if cv is not None else "hr",
    )
    return build_draft_text(facts)


def latest_application(session: Session, job: Job) -> Application | None:
    return session.scalar(
        select(Application).where(Application.job_id == job.id).order_by(Application.id.desc())
        .limit(1))


def ensure_draft(session: Session, job: Job, profile: Profile, now: datetime | None = None, *,
                 manual: bool = False, dry_run: bool = True) -> Application | None:
    """The draft for a job: the existing one, or a new one when the rules allow it, else None."""
    existing = latest_application(session, job)
    if existing is not None:
        return existing
    if why_no_draft(job, profile, now, manual=manual) is not None:
        return None
    to = ad_email(job)
    cv = _pick_cv(job, profile)
    text = draft_for(job, profile, cv)
    application = Application(
        job_id=job.id, state=ApplicationState.QUEUED.value, cv_id=cv.id if cv else None,
        to_email=to.value, subject=text.subject, body=text.body, dry_run=dry_run)
    session.add(application)
    session.flush()
    session.add(Event(kind="draft_created", job_id=job.id,
                      data={"application": application.id, "manual": manual}))
    session.flush()
    return application


def ensure_drafts(session: Session, profile: Profile, now: datetime | None = None, *,
                  dry_run: bool = True, limit: int = CATCH_UP_LIMIT) -> int:
    """Prepare the drafts that are missing for jobs that already qualify. Returns how many."""
    now = now or utcnow()
    has_application = select(Application.job_id).where(Application.job_id == Job.id).exists()
    jobs = session.scalars(
        select(Job).where(
            Job.status.in_(OPEN_JOB_STATUSES), Job.channel == "email", Job.score.is_not(None),
            Job.score >= profile.notify_threshold, Job.duplicate_of.is_(None), ~has_application,
        ).order_by(Job.score.desc(), Job.id.desc()).limit(limit)
    ).all()
    made = 0
    for job in jobs:
        if ensure_draft(session, job, profile, now, dry_run=dry_run) is not None:
            made += 1
    return made


# ---------------------------------------------------------------- editing
def _one_line_error(subject: str) -> str | None:
    if not subject.strip():
        return "Upišite naslov poruke."
    if _CONTROL.search(subject):
        return "Naslov mora biti u jednom retku."
    if len(subject) > MAX_SUBJECT:
        return f"Naslov može imati najviše {MAX_SUBJECT} znakova."
    return None


def check_text(subject: str, body: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    if (message := _one_line_error(subject)) is not None:
        errors["subject"] = message
    if not body.strip():
        errors["body"] = "Upišite tekst poruke."
    elif len(body) > MAX_BODY:
        errors["body"] = f"Tekst može imati najviše {MAX_BODY} znakova."
    return errors


def edit_draft(session: Session, application: Application, profile: Profile, *, subject: str,
               body: str, cv_id: int | None) -> dict[str, str]:
    """Change the draft. Only a draft that waits for approval can be changed."""
    if application.state != ApplicationState.QUEUED.value:
        return {"state": "Nacrt se može mijenjati samo dok čeka odobrenje. "
                         "Ako je odobren, najprije ga poništite."}
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    subject = subject.strip()
    errors = check_text(subject, body)
    if cv_id is not None and cv_id not in {c.id for c in profile.cvs}:
        errors["cv"] = "Taj životopis ne postoji."
    if errors:
        return errors
    changed = [name for name, old, new in (
        ("subject", application.subject, subject), ("body", application.body, body),
        ("cv", application.cv_id, cv_id)) if old != new]
    application.subject, application.body, application.cv_id = subject, body, cv_id
    if changed:
        session.add(Event(kind="draft_edited", job_id=application.job_id,
                          data={"application": application.id, "changed": changed}))
    session.flush()
    return {}


# ---------------------------------------------------------------- approving
def approval_problems(application: Application) -> dict[str, str]:
    """Why this draft cannot be approved yet (empty = it can)."""
    errors: dict[str, str] = {}
    if application.state != ApplicationState.QUEUED.value:
        errors["state"] = "Ovaj nacrt ne čeka odobrenje."
        return errors
    errors.update(check_text(application.subject or "", application.body or ""))
    if has_placeholder(application.subject) or has_placeholder(application.body):
        errors["body"] = ("U tekstu je ostalo mjesto koje treba popuniti, označeno s [[ ]]. "
                          "Dopunite ga pa odobrite.")
    job = application.job
    allowed = {c.value.lower() for c in job.contacts
               if c.kind == ContactKind.EMAIL.value and c.from_ad}
    recipient = (application.to_email or "").lower()
    if not valid_address(application.to_email) or recipient not in allowed:
        errors["to"] = "Poruka se šalje samo na e-adresu koja piše u samom oglasu."
    if job.status not in OPEN_JOB_STATUSES:
        errors["state"] = "Oglas više nije među otvorenima."
    return errors


def approve(session: Session, application: Application, profile: Profile,
            now: datetime | None = None, *, dry_run: bool = True) -> dict[str, str]:
    """Approve THIS text. Sets the undo window; nothing is sent here.

    `dry_run` is the mode of the program at this moment; an approval given in dry-run mode is
    refused by the gate once real sending is on (see `gate.check_sendable`).
    """
    now = now or utcnow()
    errors = approval_problems(application)
    if errors:
        return errors
    application.state = ApplicationState.APPROVED.value
    application.approved_at = now
    application.approved_hash = draft_fingerprint(application)
    application.send_after = now + timedelta(minutes=profile.apply_undo_minutes)
    application.dry_run, application.dry_run_at, application.error = dry_run, None, None
    session.add(Event(kind="application_approved", job_id=application.job_id, data={
        "application": application.id, "send_after": application.send_after.isoformat()}))
    session.flush()
    return {}


def can_undo(application: Application, now: datetime | None = None) -> bool:
    now = now or utcnow()
    send_after = _aware(application.send_after)
    return (application.state == ApplicationState.APPROVED.value and application.sent_at is None
            and send_after is not None and now < send_after)


def undo(session: Session, application: Application, now: datetime | None = None) -> str | None:
    """Take an approval back inside the undo window. Returns an error message or None."""
    if not can_undo(application, now):
        return "Rok za poništavanje je istekao ili poruka nije odobrena."
    application.state = ApplicationState.QUEUED.value
    application.approved_at = application.approved_hash = application.send_after = None
    application.dry_run_at = None
    session.add(Event(kind="application_undone", job_id=application.job_id,
                      data={"application": application.id}))
    session.flush()
    return None


def release_dry_run_approval(session: Session, application: Application) -> str | None:
    """An approval given in dry-run mode goes back to "waiting for approval", so that Ivan approves
    that text again once real sending is on. Allowed at any time before a real send."""
    if (application.state != ApplicationState.APPROVED.value or application.sent_at is not None
            or not application.dry_run):
        return "Ovo odobrenje nije dobiveno u probnom načinu."
    application.state = ApplicationState.QUEUED.value
    application.approved_at = application.approved_hash = application.send_after = None
    application.dry_run_at = None
    session.add(Event(kind="application_released", job_id=application.job_id,
                      data={"application": application.id}))
    session.flush()
    return None


def skip(session: Session, application: Application) -> str | None:
    if application.state != ApplicationState.QUEUED.value:
        return "Preskočiti se može samo nacrt koji čeka odobrenje."
    application.state = ApplicationState.SKIPPED.value
    session.add(Event(kind="application_skipped", job_id=application.job_id,
                      data={"application": application.id}))
    session.flush()
    return None


def reopen(session: Session, application: Application) -> str | None:
    if application.state != ApplicationState.SKIPPED.value:
        return "Vratiti se može samo preskočen nacrt."
    application.state = ApplicationState.QUEUED.value
    session.add(Event(kind="application_reopened", job_id=application.job_id,
                      data={"application": application.id}))
    session.flush()
    return None


CONFIRMABLE = (ApplicationState.SENT.value, ApplicationState.REPLIED.value,
               ApplicationState.INTERVIEW.value)


def confirm(session: Session, application: Application) -> str | None:
    """Ivan marks that the employer said yes (plan 5.4b). Student servis stays outside the app."""
    if application.state not in CONFIRMABLE:
        return "Potvrda ima smisla tek kad je poruka poslana."
    application.state = ApplicationState.CONFIRMED.value
    session.add(Event(kind="application_confirmed", job_id=application.job_id,
                      data={"application": application.id}))
    session.flush()
    return None


# ---------------------------------------------------------------- safety numbers, kill switch
@dataclass
class Rules:
    daily_cap: int
    cooldown_days: int
    undo_minutes: int


def _int_in(raw: str | None, low: int, high: int, name: str, errors: dict[str, str],
            key: str) -> int | None:
    try:
        value = int((raw or "").strip())
    except ValueError:
        errors[key] = f"{name}: upišite cijeli broj."
        return None
    if not low <= value <= high:
        errors[key] = f"{name}: broj mora biti između {low} i {high}."
        return None
    return value


def update_rules(session: Session, profile: Profile, form: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    cap = _int_in(form.get("daily_cap"), *CAP_CHOICES, "Najviše prijava na dan", errors,
                  "daily_cap")
    days = _int_in(form.get("cooldown_days"), *COOLDOWN_CHOICES, "Razmak prema istom poslodavcu",
                   errors, "cooldown_days")
    undo = _int_in(form.get("undo_minutes"), *UNDO_CHOICES, "Vrijeme za poništavanje", errors,
                   "undo_minutes")
    if errors:
        return errors
    profile.apply_daily_cap, profile.apply_cooldown_days = cap, days
    profile.apply_undo_minutes = undo
    session.add(Event(kind="apply_rules_changed", data={
        "daily_cap": cap, "cooldown_days": days, "undo_minutes": undo, "by": "user"}))
    session.flush()
    return {}


def set_paused(session: Session, profile: Profile, paused: bool) -> bool:
    """The kill switch. True if the state changed."""
    if bool(profile.sending_paused) == paused:
        return False
    profile.sending_paused = paused
    session.add(Event(kind="sending_paused" if paused else "sending_resumed",
                      data={"by": "user"}))
    session.flush()
    return True
