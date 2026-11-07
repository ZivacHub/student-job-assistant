"""Approve or cancel an application by replying to its alert (plan 5.3b, D42).

Ivan replies "DA" (or "DA" with a new text below it, or "NE") to the alert, or to the "approved"
mail that follows. A reply counts only if ALL of these hold; the checks run in this order and the
text of the mail is fetched only after the first four passed:
  1. it comes from Ivan's own address;
  2. Gmail's own verdict says the mail really was sent by that address (`replyparse`);
  3. it answers a mail this program sent for a code (In-Reply-To / References);
  4. the code in its title is a real one, and the code is not used up or expired;
  5. the draft is still the one the alert showed (a changed draft is never approved by a reply);
  6. the reply says DA or NE in its first line.
Anything else is ignored and written to the log with the reason (never with the mail's text).
Approving goes through `service.approve`, so the undo window, the daily limit, the per-employer
pause, the kill switch and trial mode apply exactly as for the button on the Prijave page. Nothing
here ever sends to an employer: only mails to Ivan himself.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.apply import replycode, service
from app.apply.replyparse import (
    EMPTY,
    NO,
    SHORT,
    UNCLEAR,
    read_command,
    sender_check_passed,
    verdict_summary,
)
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.models import Application, ApprovalCode, ApprovalReply, Event, Profile
from app.models.base import utcnow
from app.notify.dispatch import (
    PushFactory,
    SenderFactory,
    log_failure,
    push_factory_for,
    push_to_phone,
    sender_factory_for,
)
from app.notify.gmail import GmailError, GmailSender
from app.notify.imapbox import MailHead, MailReadError, MailSession, build_reader
from app.notify.secrets import SecretStore

log = logging.getLogger(__name__)

ReaderFactory = Callable[[Profile], AbstractContextManager[MailSession] | None]

# reasons a reply was not acted on. The first group is never answered (a stranger gets no reply
# from this program); the second group is a mistake of Ivan's own, so he gets a short mail.
WRONG_SENDER = "wrong_sender"
NOT_AUTHENTICATED = "not_authenticated"
NOT_A_REPLY = "not_a_reply"
UNKNOWN_CODE = "unknown_code"
REPLAYED = "replayed"
EXPIRED = "expired"
DRAFT_CHANGED = "draft_changed"
NOT_WAITING = "not_waiting"
BAD_TEXT = "bad_text"
TOO_LATE = "too_late"
APPROVED = "approved"
CANCELLED = "cancelled"
SKIPPED = "skipped"

NOTICES = {
    EXPIRED: "Kod je istekao (vrijedi 24 sata). Odobrite poruku na stranici Prijave u aplikaciji.",
    DRAFT_CHANGED: ("Nacrt je u međuvremenu promijenjen, zato ga odgovor nije odobrio. Pogledajte "
                    "novi tekst i odobrite ga na stranici Prijave u aplikaciji."),
    NOT_WAITING: ("Ova prijava više ne čeka odobrenje (već je odobrena ili preskočena, ili je "
                  "oglas zatvoren). Ništa nije promijenjeno."),
    BAD_TEXT: ("Novi tekst nije prihvaćen (prazan, predug ili je u njemu ostalo mjesto za "
               "popunjavanje označeno s [[ ]]). Ništa nije odobreno."),
    UNCLEAR: "Odgovor nije shvaćen: u prvom retku mora biti samo DA ili NE. Ništa nije odobreno.",
    EMPTY: "Odgovor je prazan: u prvom retku napišite samo DA ili NE. Ništa nije odobreno.",
    SHORT: ("Ispod DA je vrlo kratak tekst. Napišite samo DA za poruku kakva je u obavijesti, ili "
            "DA i ispod njega cijeli novi tekst poruke. Ništa nije odobreno."),
    TOO_LATE: ("Rok za poništenje je istekao ili poruka nije bila odobrena. "
               "Ništa nije promijenjeno."),
}
MAX_TITLE = 80


@dataclass
class ReplyResult:
    examined: int = 0
    approved: int = 0
    cancelled: int = 0
    ignored: int = 0
    stopped: str | None = None  # "no_gmail", "auth", "network" or None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


def _short(text: str, limit: int = MAX_TITLE) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


# ---------------------------------------------------------------- the mails to Ivan
def _confirmation(application: Application, marker: str, *, dry_run: bool, new_text: bool,
                  undo_minutes: int) -> tuple[str, str]:
    """The "approved" mail: the exact final text, how long the undo window is, how to cancel."""
    job = application.job
    if dry_run:
        mode = ("PROBNI NAČIN: poruka se NEĆE poslati poslodavcu, samo se zapisuje u dnevnik "
                "aplikacije.")
    else:
        mode = (f"Poruka će biti poslana poslodavcu za oko {undo_minutes} min "
                "(uz dnevno ograničenje i stanku za istog poslodavca).")
    lines = [
        f"Odobreno odgovorom DA: {job.title}", "", mode,
        "Za poništenje odgovorite na ovu poruku s NE prije isteka tog roka, ili na stranici "
        "Prijave u aplikaciji.", "",
        "KONAČNI TEKST" + (" (vaš novi tekst)" if new_text else ""),
        f"Prima: {application.to_email}", f"Predmet: {application.subject}", "",
        application.body or "", "", f"Kod: {marker}",
    ]
    return f"Odobreno: {_short(job.title, 60)} [{marker}]", "\n".join(lines)


def _notice(sender: GmailSender | None, address: str, text: str) -> None:
    """A short mail to Ivan himself about a mistake of his. Best effort, never raises."""
    if sender is None:
        return
    try:
        sender.send_message(to=address, subject="Odgovor nije prihvaćen", text=text)
    except GmailError as error:
        log.warning("notice not sent (%s)", error.code)


# ---------------------------------------------------------------- one reply
def _approve(session: Session, profile: Profile, row: ApprovalCode, new_body: str | None,
             now: datetime, dry_run: bool) -> str | None:
    """Approve what the alert showed (or the new text). Returns a reason when it did not work."""
    application = session.get(Application, row.application_id)
    if application is None or application.state != "queued":
        return NOT_WAITING
    if service.draft_fingerprint(application) != row.draft_hash:
        return DRAFT_CHANGED
    if new_body is not None:
        if service.edit_draft(session, application, profile, subject=application.subject or "",
                              body=new_body, cv_id=application.cv_id):
            session.rollback()
            return BAD_TEXT
    if service.approve(session, application, profile, now, dry_run=dry_run):
        session.rollback()  # also takes the new text back: nothing is left half done
        return BAD_TEXT if new_body is not None else NOT_WAITING
    row.used_at, row.outcome = now, replycode.OUTCOME_APPROVED
    return None


def _cancel(session: Session, row: ApprovalCode, now: datetime) -> tuple[str | None, str | None]:
    """NE: skip a draft that still waits, or take back an approval inside the undo window.
    Returns (outcome, reason)."""
    application = session.get(Application, row.application_id)
    if application is None:
        return None, NOT_WAITING
    if row.used_at is None:
        if replycode.is_expired(row, now):
            return None, EXPIRED
        if service.skip(session, application) is not None:
            return None, NOT_WAITING
        row.used_at, row.outcome = now, replycode.OUTCOME_SKIPPED
        return SKIPPED, None
    if row.outcome == replycode.OUTCOME_APPROVED:
        if service.undo(session, application, now) is not None:
            return None, TOO_LATE
        row.outcome = replycode.OUTCOME_CANCELLED
        return CANCELLED, None
    return None, REPLAYED


def _own_copies(heads: list[MailHead], address: str) -> dict[str, set[str]]:
    """Gmail may keep our own alert or confirmation under another Message-ID than the one we
    gave it, and replies point to the one Gmail kept. A mail from Ivan's own account that passed
    Gmail's check, holds a code and answers nothing, is such a copy: it is not handled as a reply
    and its Message-ID counts as ours for that code. Returns {code: Message-IDs}."""
    copies: dict[str, set[str]] = {}
    for head in heads:
        if (head.replies_to or head.from_address != address
                or not sender_check_passed(head.auth_results, address)):
            continue
        for code in replycode.find_codes(head.subject):
            copies.setdefault(code, set()).add(head.message_id)
    return copies


def _judge(session: Session, profile: Profile, head: MailHead, mail: MailSession, address: str,
           now: datetime, dry_run: bool, copies: dict[str, set[str]]):
    """Everything about one mail. Returns (code row or None, outcome, reason, command, new_text)."""
    if head.from_address != address:
        return None, None, WRONG_SENDER, None, False
    if not sender_check_passed(head.auth_results, address):
        return None, None, NOT_AUTHENTICATED, None, False
    row = code = None
    for found in replycode.find_codes(head.subject):
        row = replycode.find_code(session, found, now)
        if row is not None:
            code = found
            break
    if row is None:
        return None, None, UNKNOWN_CODE, None, False
    ids = {row.alert_message_id, row.confirm_message_id, *copies.get(code, ())} - {None}
    if not ids & set(head.replies_to):
        return row, None, NOT_A_REPLY, None, False
    command = read_command(mail.text(head.uid))
    if command.kind in (EMPTY, UNCLEAR, SHORT):
        return row, None, command.kind, command, False
    if command.kind == NO:
        outcome, reason = _cancel(session, row, now)
        return row, outcome, reason, command, False
    if row.used_at is not None:
        return row, None, REPLAYED, command, False
    if replycode.is_expired(row, now):
        return row, None, EXPIRED, command, False
    reason = _approve(session, profile, row, command.new_body, now, dry_run)
    outcome = APPROVED if reason is None else None
    return row, outcome, reason, command, command.new_body is not None


def process_replies(session: Session, profile: Profile, *, reader_factory: ReaderFactory,
                    sender_factory: SenderFactory, push_factory: PushFactory | None = None,
                    now: datetime | None = None, dry_run: bool = True) -> ReplyResult:
    """One round. Does nothing, and does not even connect to Gmail, while no alert waits for an
    answer. Never raises for a mail problem."""
    now = now or utcnow()
    result = ReplyResult()
    live = replycode.live_codes(session, now)
    if not live:
        return result
    reader = reader_factory(profile)
    if reader is None:
        result.stopped = "no_gmail"
        return result
    address = (profile.gmail_address or "").strip().lower()
    sender = sender_factory(profile)
    since = (_aware(min(row.created_at for row in live)) - timedelta(days=1)).date()
    try:
        with reader as mail:
            own = replycode.own_message_ids(session, now)
            heads = [h for h in mail.find(since) if h.message_id and h.message_id not in own]
            copies = _own_copies(heads, address)
            kept_by_gmail = set().union(*copies.values())
            heads = [h for h in heads if h.message_id not in kept_by_gmail]
            known = set(session.scalars(select(ApprovalReply.message_id).where(
                ApprovalReply.message_id.in_([h.message_id for h in heads]))))
            for head in heads:
                if head.message_id in known or not replycode.find_codes(head.subject):
                    continue
                result.examined += 1
                row, outcome, reason, command, new_text = _judge(
                    session, profile, head, mail, address, now, dry_run, copies)
                _record(session, head, row, outcome, reason)
                session.commit()
                if outcome in (APPROVED, SKIPPED, CANCELLED):
                    _after(session, profile, row, outcome, head, sender, push_factory, address,
                           now, dry_run, new_text)
                    result.approved += outcome == APPROVED
                    result.cancelled += outcome in (SKIPPED, CANCELLED)
                else:
                    result.ignored += 1
                    if reason in NOTICES:
                        _notice(sender, address, NOTICES[reason])
    except MailReadError as error:
        session.rollback()
        result.stopped = error.code
        log_failure(session, error, now, kind="reply_watch_failed")
        session.commit()
        log.warning("replies not read (%s)", error.code)
    return result


def _record(session: Session, head: MailHead, row: ApprovalCode | None, outcome: str | None,
            reason: str | None) -> None:
    """Remember the mail (so it is never handled twice) and write it to the log. The log holds
    the reason and the ids, never the mail's text."""
    label = outcome or reason or "ignored"
    session.add(ApprovalReply(message_id=head.message_id, code_id=row.id if row else None,
                              outcome=label))
    application = session.get(Application, row.application_id) if row is not None else None
    kind = {APPROVED: "reply_approved", SKIPPED: "reply_cancelled",
            CANCELLED: "reply_cancelled"}.get(outcome or "", "reply_ignored")
    data = {"result": label}
    if reason == NOT_AUTHENTICATED:  # what Gmail said, so a refusal explains itself
        data["verdict"] = verdict_summary(head.auth_results)
    session.add(Event(kind=kind, job_id=application.job_id if application else None,
                      application_id=application.id if application else None, data=data))


def _after(session: Session, profile: Profile, row: ApprovalCode, outcome: str, head: MailHead,
           sender: GmailSender | None, push_factory: PushFactory | None, address: str,
           now: datetime, dry_run: bool, new_text: bool) -> None:
    """The mail and the push that tell Ivan what his reply did. Best effort: the approval or the
    cancellation already stands, a failed mail is only logged."""
    application = session.get(Application, row.application_id)
    job = application.job
    if outcome == APPROVED:
        marker = replycode.marker(replycode.find_codes(head.subject)[0])
        subject, text = _confirmation(application, marker, dry_run=dry_run, new_text=new_text,
                                      undo_minutes=profile.apply_undo_minutes)
        if sender is not None:
            try:
                message_id = sender.new_message_id()
                row.confirm_message_id = message_id  # saved first: the reader skips its own mail
                session.commit()
                sender.send_message(to=address, subject=subject, text=text, message_id=message_id)
            except GmailError as error:
                log_failure(session, error, now, kind="reply_mail_failed")
                session.commit()
        push_to_phone(session, profile, push_factory, now, title="Odobreno",
                      message=f"{_short(job.title, 60)}: odgovorite NE za poništenje.",
                      priority=4)
    elif sender is not None:
        text = ("Poništeno. Nacrt opet čeka odobrenje na stranici Prijave u aplikaciji."
                if outcome == CANCELLED else
                f"Preskočeno: {job.title}. Nacrt se može vratiti na stranici Prijave.")
        try:
            sender.send_message(to=address, subject="Odgovor NE primljen", text=text)
        except GmailError as error:
            log_failure(session, error, now, kind="reply_mail_failed")
            session.commit()


def make_reply_watcher(session_factory: sessionmaker[Session], *,
                       store_getter: Callable[[], SecretStore]) -> Callable[[], ReplyResult]:
    """What the scheduler calls once a minute: one round with its own database session."""

    def run() -> ReplyResult:
        with session_factory() as session:
            profile = get_or_create_profile(session)
            store = store_getter()
            return process_replies(
                session, profile, reader_factory=lambda p: build_reader(p, store),
                sender_factory=sender_factory_for(store), push_factory=push_factory_for(store),
                dry_run=get_settings().dry_run)

    return run
