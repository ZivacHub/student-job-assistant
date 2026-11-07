"""One-time approval codes for "reply DA" (plan 5.3b, D42).

An alert that shows a drafted application carries a code like `K7M2-Q9XA`; its marker
`SJA-K7M2-Q9XA` is put at the end of the alert's title, so the phone's reply ("Re: ...") keeps it.
The code stays valid for 24 hours and is used once. Only a salted fingerprint is stored, never the
code itself, and it is bound to the exact draft the alert showed (`draft_hash`).
"""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.apply import service
from app.models import Application, ApplicationState, ApprovalCode
from app.models.base import utcnow

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0, O, 1, I, L: nothing to mix up on a phone
CODE_TTL = timedelta(hours=24)
LOOKUP_DAYS = 14  # an expired code is still recognised (to say so), not for ever
PREFIX = "SJA-"
_CODE = re.compile(r"(?<![A-Z0-9])SJA-([A-HJKMNP-Z2-9]{4})-([A-HJKMNP-Z2-9]{4})(?![A-Z0-9])")

OUTCOME_APPROVED = "approved"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_SKIPPED = "skipped"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)  # SQLite hands back naive UTC


def new_code() -> str:
    """8 letters and digits in two groups of four, e.g. `K7M2-Q9XA` (about 40 bits)."""
    chars = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def marker(code: str) -> str:
    """What goes into the title and the text of the alert: `SJA-K7M2-Q9XA`."""
    return PREFIX + code


def find_codes(text: str | None) -> list[str]:
    """The codes written in a text (a title or a mail body), in order, upper-cased."""
    return [f"{a}-{b}" for a, b in _CODE.findall((text or "").upper())]


def _fingerprint(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code.upper()}".encode()).hexdigest()


def issue_code(session: Session, application: Application, *, alert_message_id: str,
               now: datetime | None = None) -> str:
    """Make a new code for the alert that is about to be sent. Returns the code (shown once)."""
    now = now or utcnow()
    code, salt = new_code(), secrets.token_hex(8)
    session.add(ApprovalCode(
        application_id=application.id, code_salt=salt, code_hash=_fingerprint(salt, code),
        alert_message_id=alert_message_id, draft_hash=service.draft_fingerprint(application),
        created_at=now, expires_at=now + CODE_TTL))
    session.flush()
    return code


def mark_sent(session: Session, application_id: int, alert_message_id: str,
              now: datetime | None = None) -> None:
    """Gmail took the alert: from now on its code is live."""
    row = session.scalar(select(ApprovalCode).where(
        ApprovalCode.application_id == application_id,
        ApprovalCode.alert_message_id == alert_message_id))
    if row is not None:
        row.sent_at = now or utcnow()
        session.flush()


def find_code(session: Session, code: str, now: datetime | None = None) -> ApprovalCode | None:
    """The row of this code, or None. Looks only at recent, delivered codes."""
    now = now or utcnow()
    rows = session.scalars(select(ApprovalCode).where(
        ApprovalCode.sent_at.is_not(None),
        ApprovalCode.created_at >= now - timedelta(days=LOOKUP_DAYS))).all()
    for row in rows:
        if hmac.compare_digest(row.code_hash, _fingerprint(row.code_salt, code)):
            return row
    return None


def is_expired(row: ApprovalCode, now: datetime | None = None) -> bool:
    return (now or utcnow()) >= _aware(row.expires_at)


def is_live(session: Session, row: ApprovalCode, now: datetime | None = None) -> bool:
    """A code still worth listening for: unused, not expired, and its draft still waits; or used to
    approve a draft whose undo window is still open (a reply "NE" can take that back). A draft
    already approved, skipped or sent some other way waits for no answer."""
    now = now or utcnow()
    if row.sent_at is None:
        return False
    application = session.get(Application, row.application_id)
    if application is None:
        return False
    if row.used_at is None:
        return application.state == ApplicationState.QUEUED.value and not is_expired(row, now)
    return (row.outcome == OUTCOME_APPROVED
            and application.state == ApplicationState.APPROVED.value
            and service.can_undo(application, now))


def live_codes(session: Session, now: datetime | None = None) -> list[ApprovalCode]:
    now = now or utcnow()
    rows = session.scalars(select(ApprovalCode).where(
        ApprovalCode.sent_at.is_not(None),
        ApprovalCode.created_at >= now - timedelta(days=LOOKUP_DAYS))).all()
    return [row for row in rows if is_live(session, row, now)]


def own_message_ids(session: Session, now: datetime | None = None) -> set[str]:
    """The Message-IDs of the mails this program sent for the codes (alerts and confirmations)."""
    now = now or utcnow()
    rows = session.scalars(select(ApprovalCode).where(
        ApprovalCode.created_at >= now - timedelta(days=LOOKUP_DAYS))).all()
    ids = {row.alert_message_id for row in rows}
    ids.update(row.confirm_message_id for row in rows if row.confirm_message_id)
    return ids
