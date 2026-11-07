"""The approval gate (plan 5.3) and the safety rules (5.4): the ONE place that may send an email.

Hard rule (Ivan, 2026-10-14, D9): nothing is sent without his approval of that exact draft.
`send_application` calls the sender only when `check_sendable` finds nothing wrong. Every rule
below has a test that proves it blocks (tests/test_apply_gate.py), including "no approval means
the sender is never called".

The rules, all of which must hold for a real send:
  - the application is APPROVED, was never sent before, and carries an approval;
  - the approval matches the draft as it is now (address, subject, text, CV): an edit made after
    approving, even directly in the database, cancels the approval;
  - the undo window is over;
  - the address is one printed in the ad itself, and nothing in it can add headers;
  - the ad is still open (not ignored or applied meanwhile);
  - the kill switch is off;
  - the approval was not given in dry-run mode (then Ivan approves again);
  - the daily cap and the per-employer cooldown are not used up;
  - the program is not in dry-run mode (then the send is only logged);
  - a sender exists (Gmail is set up) and the CV file is there.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.apply.service import OPEN_JOB_STATUSES, _aware, draft_fingerprint, valid_address
from app.models import (
    Application,
    ApplicationState,
    ContactKind,
    Cv,
    Event,
    JobStatus,
    Profile,
)
from app.models.base import utcnow
from app.scoring.dedupe import clean

log = logging.getLogger(__name__)

_SEND_LOCK = threading.Lock()  # one sending run at a time inside this program
LOCAL_TZ = ZoneInfo("Europe/Zagreb")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BAD_ADDRESS_CHARS = re.compile(r"[\s,;<>()\[\]\\\"']")

# reason codes (the text is for the page; the code is what the tests and the log look at)
NO_APPROVAL = "no_approval"
WRONG_STATE = "wrong_state"
ALREADY_SENT = "already_sent"
CHANGED = "changed_after_approval"
UNDO_WINDOW = "undo_window"
ADDRESS = "address_not_in_ad"
UNSAFE_HEADER = "unsafe_header"
JOB_CLOSED = "job_not_open"
KILL_SWITCH = "kill_switch"
APPROVED_IN_DRY_RUN = "approved_in_dry_run"
DAILY_CAP = "daily_cap"
COOLDOWN = "cooldown"
CV_MISSING = "cv_missing"
NOT_CONFIGURED = "not_configured"
DRY_RUN = "dry_run"


@dataclass(frozen=True)
class Reason:
    code: str
    text: str  # plain Croatian, shown to Ivan


@dataclass
class Verdict:
    reasons: list[Reason] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons

    @property
    def codes(self) -> list[str]:
        return [r.code for r in self.reasons]

    @property
    def dry_run_only(self) -> bool:
        """Everything is in order except that the program is in dry-run mode."""
        return self.codes == [DRY_RUN]


class Sender(Protocol):
    """What the gate needs from a mail sender. The real one (Gmail, app password) comes with the
    Gmail setup (plan 3.2); tests use a recording stand-in."""

    def send(self, *, to: str, subject: str, body: str, attachment: Path | None) -> str:
        """Send the message; return its Message-ID. Raise on failure."""
        ...


@dataclass
class Outcome:
    kind: str  # "sent" | "dry_run" | "blocked" | "failed"
    reasons: list[Reason] = field(default_factory=list)


def _local_midnight(now: datetime) -> datetime:
    local = now.astimezone(LOCAL_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def employer_key(application: Application) -> str:
    return clean(application.job.employer or "")


def _others_since(session: Session, application: Application, since: datetime, *,
                  include_dry_run: bool) -> list[Application]:
    """Applications (not this one) that went out since `since`; in dry-run mode the logged
    dry runs count too, so a dry-run day shows what the rules would have done."""
    rows = session.scalars(select(Application).where(Application.id != application.id)).all()
    out = []
    for other in rows:
        sent = _aware(other.sent_at)
        dry = _aware(other.dry_run_at)
        if (sent is not None and sent >= since) or (
                include_dry_run and dry is not None and dry >= since):
            out.append(other)
    return out


def check_sendable(session: Session, application: Application, profile: Profile, *,
                   dry_run: bool, now: datetime | None = None, sender_ready: bool = True,
                   cv_found: bool = True) -> Verdict:
    """Every reason why this application must NOT be sent right now (empty = it may be)."""
    now = now or utcnow()
    verdict = Verdict()

    def refuse(code: str, text: str) -> None:
        verdict.reasons.append(Reason(code, text))

    if application.sent_at is not None or application.state in (
            ApplicationState.SENT.value, ApplicationState.REPLIED.value,
            ApplicationState.CONFIRMED.value, ApplicationState.INTERVIEW.value):
        refuse(ALREADY_SENT, "Ova poruka je već poslana.")
    elif application.state != ApplicationState.APPROVED.value:
        refuse(WRONG_STATE, "Poruka nije u stanju odobrenja.")
    if application.approved_at is None or not application.approved_hash:
        refuse(NO_APPROVAL, "Poruku niste odobrili.")
    elif application.approved_hash != draft_fingerprint(application):
        refuse(CHANGED, "Tekst, naslov, primatelj ili životopis promijenjeni su nakon odobrenja.")
    send_after = _aware(application.send_after)
    if send_after is None or now < send_after:
        refuse(UNDO_WINDOW, "Rok za poništavanje još traje.")

    job = application.job
    to = application.to_email or ""
    in_ad = {c.value.lower() for c in job.contacts
             if c.kind == ContactKind.EMAIL.value and c.from_ad}
    if not valid_address(to) or to.lower() not in in_ad:
        refuse(ADDRESS, "E-adresa nije ona koja piše u oglasu.")
    if _CONTROL.search(application.subject or "") or _BAD_ADDRESS_CHARS.search(to):
        refuse(UNSAFE_HEADER, "Naslov ili adresa sadrže znakove koji nisu dopušteni.")
    if job.status not in OPEN_JOB_STATUSES:
        refuse(JOB_CLOSED, "Oglas više nije među otvorenima (zanemaren ili prijavljen).")
    if profile.sending_paused:
        refuse(KILL_SWITCH, "Slanje je zaustavljeno tipkom „Zaustavi slanje“.")
    if application.dry_run and not dry_run:
        refuse(APPROVED_IN_DRY_RUN,
               "Odobreno je u probnom načinu; odobrite ponovno kad je slanje uključeno.")

    cap = profile.apply_daily_cap
    today = _others_since(session, application, _local_midnight(now), include_dry_run=dry_run)
    if len(today) >= cap:
        refuse(DAILY_CAP, f"Danas je već iskorišteno najviše prijava ({cap}).")
    days = profile.apply_cooldown_days
    if days > 0:
        recent = _others_since(session, application, now - timedelta(days=days),
                               include_dry_run=dry_run)
        mine = employer_key(application)
        for other in recent:
            same_address = (other.to_email or "").lower() == to.lower()
            same_employer = bool(mine) and employer_key(other) == mine
            if same_address or same_employer:
                refuse(COOLDOWN, f"Istom poslodavcu nije prošlo {days} dana od zadnje poruke.")
                break

    if not dry_run:
        if not sender_ready:
            refuse(NOT_CONFIGURED, "Slanje e-pošte još nije postavljeno (Gmail).")
        if not cv_found:
            refuse(CV_MISSING, "Datoteka životopisa nije pronađena.")
    if dry_run:
        refuse(DRY_RUN, "Probni način: poruka se samo zapisuje, ništa se ne šalje.")
    return verdict


def _log(session: Session, application: Application, kind: str, **data) -> None:
    session.add(Event(kind=kind, job_id=application.job_id,
                      data={"application": application.id, **data}))


def send_application(session: Session, application: Application, profile: Profile,
                     sender: Sender | None, *, dry_run: bool, cv_dir: Path | None = None,
                     now: datetime | None = None) -> Outcome:
    """Send one approved application, if and only if the gate allows it.

    In dry-run mode a send that would be allowed is only logged. The sender is called at most once
    and never when `check_sendable` finds a reason."""
    now = now or utcnow()
    attachment = _attachment(session, application, cv_dir)
    cv_found = application.cv_id is None or attachment is not None
    verdict = check_sendable(session, application, profile, dry_run=dry_run, now=now,
                             sender_ready=sender is not None, cv_found=cv_found)
    if verdict.dry_run_only:
        application.dry_run_at, application.error = now, None
        _log(session, application, "application_dry_run", to=application.to_email)
        session.flush()
        return Outcome("dry_run")
    if not verdict.ok:
        message = " ".join(r.text for r in verdict.reasons)
        if application.error != message:  # log a change of reason once, not on every tick
            application.error = message
            _log(session, application, "application_blocked", reasons=verdict.codes)
        session.flush()
        return Outcome("blocked", verdict.reasons)
    assert sender is not None  # check_sendable refuses NOT_CONFIGURED without one
    if not _claim(session, application, now):
        # another run (the page, the scheduler) took it a moment ago: never send twice
        return Outcome("blocked", [Reason(ALREADY_SENT, "Ova poruka se već šalje ili je poslana.")])
    try:
        message_id = sender.send(to=application.to_email, subject=application.subject,
                                 body=application.body, attachment=attachment)
    except Exception as exc:
        log.exception("sending application %s failed", application.id)
        application.state = ApplicationState.FAILED.value
        application.error = (f"Slanje nije uspjelo ({type(exc).__name__}). Provjerite mapu "
                             "Poslano u Gmailu prije nego što pokušate ponovno.")
        _log(session, application, "application_failed", error=type(exc).__name__)
        session.commit()
        return Outcome("failed", [Reason("send_failed", application.error)])
    application.state = ApplicationState.SENT.value
    application.sent_at, application.message_id, application.error = now, message_id, None
    application.job.status = JobStatus.APPLIED.value
    application.job.snoozed_until = None
    _log(session, application, "application_sent", to=application.to_email)
    session.add(Event(kind="job_applied", job_id=application.job_id,
                      data={"from": "open", "by": "application", "application": application.id}))
    session.commit()  # saved at once: a crash right after sending must not allow a second send
    return Outcome("sent")


def _claim(session: Session, application: Application, now: datetime) -> bool:
    """Take the application for sending: approved -> sending, in ONE atomic step that is saved
    before anything is sent. Only the run that wins the claim may send, so one application can
    never go out twice, however many runs overlap. A crash after the claim leaves it in
    "sending": it is never sent again by itself (a human looks at the Poslano folder)."""
    result = session.execute(
        update(Application)
        .where(Application.id == application.id,
               Application.state == ApplicationState.APPROVED.value,
               Application.sent_at.is_(None))
        .values(state=ApplicationState.SENDING.value, sending_at=now, error=None))
    session.commit()
    session.refresh(application)
    return result.rowcount == 1


def _attachment(session: Session, application: Application, cv_dir: Path | None) -> Path | None:
    """The CV file of this application. Only the file NAME of the stored path is used and it is
    looked up in the CV folder, so a stored path can never point anywhere else."""
    cv = session.get(Cv, application.cv_id) if application.cv_id is not None else None
    if cv is None or cv_dir is None:
        return None
    name = Path(cv.path.replace("\\", "/")).name
    path = (cv_dir / name).resolve() if name else None
    return path if path is not None and path.is_file() else None


def process_due(session: Session, profile: Profile, sender: Sender | None, *, dry_run: bool,
                cv_dir: Path | None = None, now: datetime | None = None,
                limit: int = 20) -> list[Outcome]:
    """Handle the approved applications whose undo window is over (oldest first). One run at a
    time inside this program; `_claim` protects against any other overlap."""
    with _SEND_LOCK:
        return _process_due(session, profile, sender, dry_run=dry_run, cv_dir=cv_dir, now=now,
                            limit=limit)


def _process_due(session: Session, profile: Profile, sender: Sender | None, *, dry_run: bool,
                 cv_dir: Path | None, now: datetime | None, limit: int) -> list[Outcome]:
    now = now or utcnow()
    due = session.scalars(
        select(Application).where(
            Application.state == ApplicationState.APPROVED.value, Application.sent_at.is_(None),
            Application.dry_run_at.is_(None), Application.send_after <= now,
        ).order_by(Application.send_after, Application.id).limit(limit)
    ).all()
    return [send_application(session, a, profile, sender, dry_run=dry_run, cv_dir=cv_dir,
                             now=now) for a in due]
