"""The page of one ad (plan 4.3): everything about it, in plain Croatian, from stored data.

Reads only. Everything that comes from the ad is untrusted text: the template escapes it. The
only links taken from an ad are its own address and addresses printed in it, and only http(s);
phone numbers become `tel:` links only when they are plain digits.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.apply import service as apply_service
from app.core.inbox import JobRow, build_row, safe_http_url
from app.core.job_actions import available_actions
from app.extract.company import companywall_search_url, get_company
from app.models import Application, Company, Job, JobStatus, Profile
from app.models.base import utcnow
from app.notify.alerts import Draft, build_alert, local_time, phone_display, reasons_hr
from app.scoring.parse import CATEGORY_HR

PHONE_DIGITS = re.compile(r"^\+?[0-9]{6,15}$")


@dataclass
class ContactItem:
    value: str
    shown: str  # how it is displayed
    href: str | None  # only set when it is safe to make clickable
    from_ad: bool  # False = typed in by Ivan (CompanyWall), not printed in the ad


@dataclass
class JobDetail:
    job: Job
    row: JobRow
    category: str | None
    description: str | None
    posted: str | None
    expires: str | None
    reasons: list[str]
    emails: list[ContactItem] = field(default_factory=list)
    phones: list[ContactItem] = field(default_factory=list)
    links: list[ContactItem] = field(default_factory=list)
    company: Company | None = None
    companywall_url: str | None = None
    alert_text: str = ""
    status_note: str | None = None
    actions: tuple[str, ...] = ()
    application: Application | None = None  # the draft of an application email, if there is one
    application_label: str | None = None
    can_draft: bool = False  # Ivan may ask for a draft by hand
    draft_reason: str | None = None  # why none was prepared automatically


def _status_note(job: Job, row: JobRow) -> str | None:
    if row.set_aside:
        return f"Ne odgovara vašem profilu: {row.set_aside}."
    if job.status == JobStatus.IGNORED.value:
        return "Zanemareno."
    if job.status == JobStatus.APPLIED.value:
        return "Označeno kao prijavljeno."
    if job.status == JobStatus.SNOOZED.value:
        until = local_time(job.snoozed_until)
        return f"Odgođeno do {until}." if until else "Odgođeno."
    return None


def _contacts(job: Job) -> tuple[list[ContactItem], list[ContactItem], list[ContactItem]]:
    ordered = sorted(job.contacts, key=lambda c: (-c.confidence, c.id or 0))
    emails, phones, links = [], [], []
    for c in ordered:
        if c.kind == "email":
            emails.append(ContactItem(c.value, c.value, None, c.from_ad))
        elif c.kind == "phone":
            href = f"tel:{c.value}" if PHONE_DIGITS.match(c.value) else None
            phones.append(ContactItem(c.value, phone_display(c.value), href, c.from_ad))
        elif c.kind == "link":
            href = safe_http_url(c.value)
            links.append(ContactItem(c.value, c.value, href, c.from_ad))
    return emails, phones, links


def load_detail(
    session: Session,
    job_id: int,
    profile: Profile,
    *,
    base_url: str = "http://127.0.0.1:8765",
    now: datetime | None = None,
) -> JobDetail | None:
    job = session.get(Job, job_id)
    if job is None:
        return None
    now = now or utcnow()
    row = build_row(job, profile, now)
    company = get_company(session, job.employer)
    emails, phones, links = _contacts(job)
    application = apply_service.latest_application(session, job)
    can_draft = application is None and apply_service.why_no_draft(
        job, profile, now, manual=True) is None
    draft_reason = apply_service.why_no_draft(job, profile, now) if can_draft else None
    draft = None
    if application is not None and application.state in ("queued", "approved"):
        cv = next((c for c in profile.cvs if c.id == application.cv_id), None)
        draft = Draft(subject=application.subject or "", body=application.body or "",
                      to_email=application.to_email or "", cv_label=cv.label if cv else None)
    return JobDetail(
        job=job,
        row=row,
        category=CATEGORY_HR.get(job.category or "", job.category),
        description=(job.description or "").strip() or None,
        posted=local_time(job.posted_at) or local_time(job.first_seen_at),
        expires=local_time(job.expires_at),
        reasons=reasons_hr(job.score_reasons),
        emails=emails,
        phones=phones,
        links=links,
        company=company,
        companywall_url=companywall_search_url(job.employer) if job.employer else None,
        alert_text=build_alert(
            job, draft=draft, company=company, full_name=profile.full_name, base_url=base_url
        ).text,
        status_note=_status_note(job, row),
        actions=available_actions(job.status, job.channel),
        application=application,
        application_label=(apply_service.STATE_LABELS.get(application.state, application.state)
                           if application is not None else None),
        can_draft=can_draft,
        draft_reason=draft_reason,
    )
