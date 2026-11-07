"""What Ivan can do with one ad (plan 4.3): ignore it, snooze it, mark it applied, bring it back.

Every action changes the ad's status and writes an Event (the app's audit log), so there is
always a record of who decided what and when. None of these actions contacts an employer.

Words: "zanemari" = ignore, "odgodi" = snooze (the ad returns among the open ones later),
"prijavljeno" = Ivan applied himself, "nazvano" = he phoned the employer (phone-only ads),
"vrati" = put it back among the open ones.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Job, JobStatus
from app.models.base import utcnow

LOCAL_TZ = ZoneInfo("Europe/Zagreb")
MORNING_HOUR = 8
SNOOZE_CHOICES = {"3h": "Za 3 sata", "sutra": "Sutra u 8:00"}
ACTIONS = ("zanemari", "odgodi", "prijavljeno", "nazvano", "vrati")


class ActionError(ValueError):
    """The request makes no sense (unknown action, unknown snooze time). Message is Croatian."""


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def snooze_until(choice: str, now: datetime) -> datetime:
    """When a snoozed ad comes back: in 3 hours, or tomorrow at 8:00 Croatian time."""
    if choice == "3h":
        return now + timedelta(hours=3)
    if choice == "sutra":
        tomorrow = now.astimezone(LOCAL_TZ) + timedelta(days=1)
        morning = tomorrow.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
        return morning.astimezone(UTC)
    raise ActionError("Nepoznato vrijeme odgode.")


def available_actions(status: str, channel: str | None = None) -> tuple[str, ...]:
    """Which buttons make sense for an ad in this status. "Nazvao/la sam" only for an ad whose
    way to apply is a phone call."""
    call = ("nazvano",) if channel == "phone" else ()
    if status in (JobStatus.NEW.value, JobStatus.SEEN.value):
        return ("zanemari", "odgodi", "prijavljeno", *call)
    if status == JobStatus.SNOOZED.value:
        return ("vrati", "zanemari", "prijavljeno", *call)
    return ("vrati",)  # ignored or applied: only undo


def apply_action(
    session: Session,
    job: Job,
    action: str,
    *,
    snooze: str = "3h",
    now: datetime | None = None,
) -> str:
    """Do one action. Returns a short key for the message shown afterwards."""
    now = now or utcnow()
    if action not in ACTIONS:
        raise ActionError("Nepoznata radnja.")
    if action not in available_actions(job.status, job.channel):
        raise ActionError("Ta radnja nije moguća za oglas u ovom stanju.")
    before = job.status
    data: dict = {"from": before}
    if action == "zanemari":
        job.status, job.snoozed_until, kind = JobStatus.IGNORED.value, None, "job_ignored"
    elif action == "odgodi":
        until = snooze_until(snooze, now)
        job.status, job.snoozed_until, kind = JobStatus.SNOOZED.value, until, "job_snoozed"
        data["until"] = until.isoformat()
    elif action == "prijavljeno":
        job.status, job.snoozed_until, kind = JobStatus.APPLIED.value, None, "job_applied"
    elif action == "nazvano":
        job.status, job.snoozed_until, kind = JobStatus.APPLIED.value, None, "job_called"
    else:  # vrati
        job.status, job.snoozed_until, kind = JobStatus.SEEN.value, None, "job_reopened"
    session.add(Event(kind=kind, job_id=job.id, data=data))
    session.flush()
    return action


def mark_seen(session: Session, job: Job) -> bool:
    """Opening an ad's page means Ivan has seen it: 'new' becomes 'seen'. True if it changed."""
    if job.status != JobStatus.NEW.value:
        return False
    job.status = JobStatus.SEEN.value
    session.flush()
    return True


def wake_due_snoozed(session: Session, now: datetime | None = None) -> int:
    """Snoozed ads whose time has come go back among the open ones. Returns how many."""
    now = now or utcnow()
    due = session.scalars(
        select(Job).where(Job.status == JobStatus.SNOOZED.value, Job.snoozed_until <= now)
    ).all()
    for job in due:
        job.status, job.snoozed_until = JobStatus.SEEN.value, None
        session.add(Event(kind="job_unsnoozed", job_id=job.id, data={"from": "snoozed"}))
    if due:
        session.flush()
    return len(due)
