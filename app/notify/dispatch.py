"""Sends the alerts and digests that Ivan's own rules decide (plan 3.2 + 3.4, D37).

`app/notify/policy.py` decides WHICH ads deserve a message; this module sends it, to Ivan's own
saved Gmail address and nowhere else. Rules kept here:
- nothing is sent unless alerts were switched on and a password is saved;
- only ads first seen after alerts were switched on (old ads never flood the phone);
- at most `MAX_URGENT_PER_RUN` urgent alerts per run, the rest wait for the next run;
- an ad counts as alerted only AFTER Gmail accepted the message, so a failure retries later;
- a refused password switches alerts off (Google may lock the account after many refusals) and
  says so in the log; any other failure is logged at most once every 30 minutes and retried.
It never sends anything to an employer: alerts go to `profile.gmail_address`.

The phone push (D38): when ntfy is switched on, each immediate alert and each digest also sends a
one-line push, AFTER the email went out. A failed push never blocks, repeats or un-marks an
email; it is logged at most once every 30 minutes.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.apply import replycode
from app.apply import service as apply_service
from app.core.profile import get_or_create_profile
from app.extract.company import get_company
from app.models import Event, Profile
from app.models.base import utcnow
from app.notify.alerts import Draft, build_alert, build_digest, build_subject
from app.notify.gmail import GmailError, GmailSender, build_sender, valid_address
from app.notify.ntfy import NtfyError, NtfySender, build_ntfy_sender, safe_click
from app.notify.policy import mark_alerted, plan_notifications
from app.notify.secrets import SecretStore

log = logging.getLogger(__name__)

MAX_URGENT_PER_RUN = 5
FAILURE_LOG_EVERY = timedelta(minutes=30)

SenderFactory = Callable[[Profile], GmailSender | None]
PushFactory = Callable[[Profile], NtfySender | None]


@dataclass
class DispatchResult:
    sent_now: int = 0
    sent_digest: int = 0
    stopped: str | None = None  # "off", "no_gmail", "auth", "network" ... or None = all went well


def sender_factory_for(store: SecretStore) -> SenderFactory:
    return lambda profile: build_sender(profile, store)


def _draft_for(session: Session, job, profile: Profile) -> Draft | None:
    """The drafted application shown in the alert (only while it still waits or is approved)."""
    application = apply_service.latest_application(session, job)
    if application is None or application.state not in ("queued", "approved"):
        return None
    cv = next((c for c in profile.cvs if c.id == application.cv_id), None)
    return Draft(subject=application.subject or "", body=application.body or "",
                 to_email=application.to_email or "", cv_label=cv.label if cv else None)


def _approval_code(session: Session, job, draft: Draft | None, sender: GmailSender,
                   now: datetime):
    """A one-time code for an alert whose draft still waits for approval (plan 5.3b): lets Ivan
    approve it by replying "DA". Returns (application, message_id, marker), all None when the
    alert carries no draft that waits. The code is live only once Gmail has accepted the alert."""
    application = apply_service.latest_application(session, job)
    if draft is None or application is None or application.state != "queued":
        return None, None, None
    message_id = sender.new_message_id()
    code = replycode.issue_code(session, application, alert_message_id=message_id, now=now)
    return application, message_id, replycode.marker(code)


def log_failure(session: Session, error: GmailError | NtfyError, now: datetime,
                 kind: str = "alert_failed") -> None:
    """Log a failure, but at most once every 30 minutes per kind."""
    last = session.scalar(
        select(Event).where(Event.kind == kind).order_by(Event.id.desc()).limit(1))
    last_ts = last.ts if last is not None else None
    if last_ts is not None and last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=now.tzinfo)
    if last_ts is None or now - last_ts >= FAILURE_LOG_EVERY:
        session.add(Event(kind=kind, ts=now, data={"code": error.code}))


def push_to_phone(session: Session, profile: Profile, push_factory: PushFactory | None,
                  now: datetime, *, title: str, message: str, priority: int,
                  click: str | None = None) -> None:
    """The phone push for one alert. Best effort: it never raises and never touches the email."""
    if push_factory is None or not profile.ntfy_enabled:
        return
    pusher = push_factory(profile)
    if pusher is None:
        return
    try:
        pusher.publish(title=title, message=message, priority=priority, click=safe_click(click))
    except NtfyError as error:
        log_failure(session, error, now, kind="push_failed")
        session.commit()
        log.warning("phone push not sent (%s)", error.code)


def dispatch_alerts(session: Session, profile: Profile, *, sender_factory: SenderFactory,
                    push_factory: PushFactory | None = None, now: datetime | None = None,
                    base_url: str = "http://127.0.0.1:8765") -> DispatchResult:
    """One round: send what the rules say. Never raises for a mail problem."""
    now = now or utcnow()
    result = DispatchResult()
    if not profile.alerts_enabled:
        result.stopped = "off"
        return result
    address = (profile.gmail_address or "").strip()
    sender = sender_factory(profile) if valid_address(address) else None
    if sender is None:
        result.stopped = "no_gmail"
        return result

    plan = plan_notifications(session, profile, now, since=profile.alerts_enabled_at)
    try:
        for job in plan.now[:MAX_URGENT_PER_RUN]:
            draft = _draft_for(session, job, profile)
            application, message_id, marker = _approval_code(session, job, draft, sender, now)
            alert = build_alert(
                job, draft=draft,
                company=get_company(session, job.employer), full_name=profile.full_name,
                base_url=base_url, approval_marker=marker)
            sender.send_message(to=address, subject=alert.subject, text=alert.text,
                                html=alert.html, message_id=message_id)
            if message_id is not None:
                replycode.mark_sent(session, application.id, message_id, now)
            mark_alerted(session, [job], "now", profile, now)
            session.commit()  # right away: a crash later must not send this one again
            result.sent_now += 1
            push_to_phone(session, profile, push_factory, now, title="Novi posao",
                  message=build_subject(job), priority=4, click=job.url)
        if plan.digest and len(plan.now) <= MAX_URGENT_PER_RUN:
            digest = build_digest(plan.digest, base_url=base_url)
            sender.send_message(to=address, subject=digest.subject, text=digest.text,
                                html=digest.html)
            mark_alerted(session, plan.digest, "digest", profile, now)
            session.commit()
            result.sent_digest = len(plan.digest)
            push_to_phone(session, profile, push_factory, now, title="Skupna obavijest",
                  message=digest.subject + ". Pogledajte u Gmailu.", priority=3)
    except GmailError as error:
        session.rollback()
        result.stopped = error.code
        if error.code == "auth":
            profile.alerts_enabled = False
            profile.gmail_problem = str(error)[:300]
            session.add(Event(kind="alert_failed", ts=now,
                              data={"code": "auth", "switched_off": True}))
            log.warning("Gmail refused the password: alerts switched off")
        else:
            log_failure(session, error, now)
            log.warning("alert not sent (%s)", error.code)
        session.commit()
    return result


def push_factory_for(store: SecretStore) -> PushFactory:
    return lambda profile: build_ntfy_sender(store)


def make_notifier(session_factory: sessionmaker[Session], *,
                  store_getter: Callable[[], SecretStore],
                  base_url: str) -> Callable[[], DispatchResult]:
    """What the scheduler calls: one dispatch round with its own database session."""

    def run() -> DispatchResult:
        with session_factory() as session:
            profile = get_or_create_profile(session)
            store = store_getter()
            return dispatch_alerts(session, profile, sender_factory=sender_factory_for(store),
                                   push_factory=push_factory_for(store), base_url=base_url)

    return run
