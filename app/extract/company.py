"""Employer info that Ivan looks up by hand (step 2.9, decision D15).

companywall.hr forbids automatic access, so the app never fetches it. It only builds a link that
Ivan opens in his own browser, and saves the phone number / director he types in. Anything saved
here is NOT from the ad: contacts made from it carry from_ad = False.
"""

from urllib.parse import quote_plus

import phonenumbers
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Contact, Job
from app.models.base import utcnow
from app.scoring.dedupe import clean

MAX_NAME_CHARS = 200


def employer_key(name: str) -> str:
    """Same employer, different spelling: 'MOMENTUM OS d.o.o.' == 'Momentum OS doo'."""
    return clean(name)


def companywall_search_url(employer: str) -> str:
    """A web-search link limited to companywall.hr. Opened by Ivan, never fetched by the app."""
    return "https://www.google.com/search?q=" + quote_plus(f'site:companywall.hr "{employer}"')


def parse_phone(raw: str, region: str = "HR") -> str:
    """Phone number as typed by a person -> E.164. Raises ValueError with a plain message."""
    try:
        number = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        raise ValueError("That does not look like a phone number.") from None
    if not phonenumbers.is_valid_number(number):
        raise ValueError("That phone number is not valid (check the area code and the length).")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def get_company(session: Session, employer: str | None) -> Company | None:
    key = employer_key(employer or "")
    if not key:
        return None
    return session.scalar(select(Company).where(Company.name_key == key))


def _tidy(value: str | None) -> str | None:
    value = " ".join((value or "").split())[:MAX_NAME_CHARS]
    return value or None


def save_company_info(
    session: Session,
    employer: str,
    *,
    phone: str | None = None,
    director_name: str | None = None,
    director_role: str | None = None,
    note: str | None = None,
) -> Company:
    """Create or update the info for an employer.

    A field left as None stays as it was; an empty string clears it.
    """
    key = employer_key(employer)
    if not key:
        raise ValueError("The employer name is empty.")
    company = get_company(session, employer)
    if company is None:
        company = Company(name_key=key, name=_tidy(employer) or employer)
        session.add(company)
    if phone is not None:
        company.phone = parse_phone(phone) if phone.strip() else None
    if director_name is not None:
        company.director_name = _tidy(director_name)
    if director_role is not None:
        company.director_role = _tidy(director_role)
    if note is not None:
        company.note = note.strip()[:1000] or None
    company.updated_at = utcnow()
    session.flush()
    apply_company_to_jobs(session, company)
    return company


def apply_company_to_jobs(session: Session, company: Company, within_days: int = 90) -> int:
    """Give the employer's phone to its jobs as a contact that is NOT from the ad.

    A job that had no email and no phone ('site' or 'none') becomes a phone job. Returns the
    number of jobs that got the new contact.
    """
    if not company.phone:
        return 0
    from datetime import timedelta

    since = utcnow() - timedelta(days=within_days)
    changed = 0
    for job in session.scalars(select(Job).where(Job.first_seen_at >= since)):
        if employer_key(job.employer or "") != company.name_key:
            continue
        if any(c.kind == "phone" and c.value == company.phone for c in job.contacts):
            continue
        job.contacts.append(
            Contact(kind="phone", value=company.phone, confidence=0.9, from_ad=False)
        )
        if job.channel in ("site", "none"):
            job.channel = "phone"
        changed += 1
    session.flush()
    return changed
