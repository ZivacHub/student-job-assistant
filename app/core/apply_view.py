"""What the Prijave pages show (plan 5.2): the queue of drafts and the page of one draft.

Plain data for the templates; the rules live in `app/apply/`. Nothing here sends anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.apply import gate, service
from app.core.inbox import JobRow, build_row
from app.models import Application, ApplicationState, Cv, Job, JobStatus, Profile
from app.models.base import utcnow
from app.notify.alerts import local_time

S = ApplicationState

# the order of the sections on the queue page
SECTIONS = (
    ("waiting", "Čeka Vaše odobrenje"),
    ("approved", "Odobreno, čeka se kraj roka za poništavanje"),
    ("dry_run", "Probno slanje (ništa nije poslano)"),
    ("sent", "Poslano"),
    ("failed", "Slanje nije uspjelo"),
    ("skipped", "Preskočeno"),
)


@dataclass
class QueueRow:
    application: Application
    job: JobRow
    state_label: str
    note: str | None  # one short line under the title


@dataclass
class Queue:
    sections: list[tuple[str, str, list[QueueRow]]]
    waiting: int
    total: int


def _section(application: Application, now: datetime | None = None) -> str:
    state = application.state
    if state == S.QUEUED.value:
        return "waiting"
    if state == S.APPROVED.value:
        return "dry_run" if application.dry_run_at is not None else "approved"
    if state == S.SENDING.value:
        return "failed" if sending_is_stuck(application, now) else "approved"
    if state in (S.SENT.value, S.REPLIED.value, S.CONFIRMED.value, S.INTERVIEW.value,
                 S.REJECTED.value):
        return "sent"
    if state == S.FAILED.value:
        return "failed"
    return "skipped"


STUCK_AFTER = timedelta(minutes=10)


def sending_is_stuck(application: Application, now: datetime | None = None) -> bool:
    """"Sending" for more than 10 minutes: the app stopped halfway. Nobody can tell from here
    whether the message left, so it is shown as unclear and never sent again by itself."""
    at = application.sending_at
    if at is None:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (now or utcnow()) - at > STUCK_AFTER


def _note(application: Application, now: datetime) -> str | None:
    if application.state == S.SENDING.value:
        if sending_is_stuck(application, now):
            return ("Nije jasno je li poruka poslana (program se zaustavio usred slanja). "
                    "Provjerite mapu Poslano u Gmailu.")
        return "Šalje se upravo sada."
    if application.state == S.APPROVED.value and application.dry_run_at is not None:
        return f"Probno zapisano {local_time(application.dry_run_at)}; ništa nije poslano."
    if application.state == S.APPROVED.value and service.can_undo(application, now):
        return f"Možete poništiti do {local_time(application.send_after)}."
    if application.state == S.SENT.value and application.sent_at is not None:
        return f"Poslano {local_time(application.sent_at)}."
    if application.state == S.FAILED.value:
        return application.error
    return None


def load_queue(session: Session, profile: Profile, now: datetime | None = None) -> Queue:
    now = now or utcnow()
    applications = session.scalars(
        select(Application).join(Job).order_by(Job.score.desc().nulls_last(), Application.id.desc())
    ).all()
    grouped: dict[str, list[QueueRow]] = {key: [] for key, _ in SECTIONS}
    for application in applications:
        grouped[_section(application, now)].append(QueueRow(
            application=application, job=build_row(application.job, profile, now),
            state_label=service.STATE_LABELS.get(application.state, application.state),
            note=_note(application, now)))
    return Queue(
        sections=[(key, label, grouped[key]) for key, label in SECTIONS if grouped[key]],
        waiting=len(grouped["waiting"]), total=len(applications))


def waiting_count(session: Session) -> int:
    """Drafts that wait for Ivan's approval for an ad that is still open (for the Inbox hint)."""
    return session.scalar(
        select(func.count()).select_from(Application).join(Job).where(
            Application.state == S.QUEUED.value,
            Job.status.in_((JobStatus.NEW.value, JobStatus.SEEN.value)))) or 0


# ---------------------------------------------------------------- one draft
@dataclass
class CvChoice:
    id: int
    label: str
    language: str
    selected: bool


@dataclass
class ApplicationView:
    application: Application
    job: JobRow
    state_label: str
    editable: bool
    can_approve: bool
    can_undo: bool
    undo_until: str | None
    dry_run_logged_at: str | None
    sent_at: str | None
    cvs: list[CvChoice]
    cv_label: str | None
    blockers: list[str] = field(default_factory=list)  # why an approved draft is not out yet
    problems: dict[str, str] = field(default_factory=dict)  # why it cannot be approved yet
    reminder: str | None = None  # after "the employer said yes"
    can_release: bool = False  # an approval given in dry-run mode can be taken back at any time
    can_confirm: bool = False
    can_skip: bool = False
    can_reopen: bool = False
    sending_stuck: bool = False  # "sending" for over 10 minutes: unclear whether it left


def load_view(session: Session, application: Application, profile: Profile, *, dry_run: bool,
              now: datetime | None = None) -> ApplicationView:
    now = now or utcnow()
    state = application.state
    cvs = [CvChoice(c.id, c.label, c.language, c.id == application.cv_id) for c in profile.cvs]
    cv = session.get(Cv, application.cv_id) if application.cv_id is not None else None
    problems = service.approval_problems(application) if state == S.QUEUED.value else {}
    blockers: list[str] = []
    if state == S.APPROVED.value and application.dry_run_at is None:
        verdict = gate.check_sendable(session, application, profile, dry_run=dry_run, now=now)
        blockers = [r.text for r in verdict.reasons
                    if r.code not in (gate.UNDO_WINDOW, gate.DRY_RUN, gate.NOT_CONFIGURED)]
    return ApplicationView(
        application=application,
        sending_stuck=state == S.SENDING.value and sending_is_stuck(application, now),
        job=build_row(application.job, profile, now),
        state_label=service.STATE_LABELS.get(state, state),
        editable=state == S.QUEUED.value,
        can_approve=state == S.QUEUED.value,
        can_undo=service.can_undo(application, now),
        undo_until=local_time(application.send_after) if application.send_after else None,
        dry_run_logged_at=local_time(application.dry_run_at) if application.dry_run_at else None,
        sent_at=local_time(application.sent_at) if application.sent_at else None,
        cvs=cvs, cv_label=cv.label if cv else None, blockers=blockers, problems=problems,
        reminder=service.CONFIRMED_REMINDER if state == S.CONFIRMED.value else None,
        can_release=(state == S.APPROVED.value and application.sent_at is None
                     and bool(application.dry_run)),
        can_confirm=state in service.CONFIRMABLE,
        can_skip=state == S.QUEUED.value,
        can_reopen=state == S.SKIPPED.value,
    )
