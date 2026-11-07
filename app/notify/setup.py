"""The steps of the Gmail setup, kept apart from the web page (plan 3.2 + 3.5, D37).

Every function works on the profile and the secret store, never prints or returns the password,
and returns plain-Croatian errors for the page. The page (`app/web/gmail_routes.py`) only calls
them.
"""

import hashlib
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Profile
from app.models.base import utcnow
from app.notify.gmail import (
    GmailError,
    GmailSender,
    build_sender,
    clean_app_password,
    valid_address,
)
from app.notify.ntfy import NTFY_ACCOUNT, NtfyError, build_ntfy_sender, new_topic, valid_topic
from app.notify.secrets import SecretStore, SecretStoreError

SmtpFactory = Callable[..., smtplib.SMTP]

TEST_SUBJECT = "Probna poruka: Studentski poslovi"
TEST_TEXT = (
    "Ovo je probna poruka iz aplikacije Studentski poslovi.\n\n"
    "Ako je vidite u Gmailu, veza radi. Tako će izgledati i obavijesti o novim oglasima.\n"
    "Ako Vam je mobitel zazvonio ili zavibrirao kad je stigla, javite to aplikaciji na "
    "stranici Gmail.\n"
)


def _tag(address: str) -> str:
    """A short fingerprint of the address, so a test message counts only for THAT address (the
    address itself stays out of the log)."""
    return hashlib.sha256(address.lower().encode("utf-8")).hexdigest()[:12]


@dataclass
class GmailStatus:
    address: str | None
    password_saved: bool
    store_error: str | None
    checked_at: datetime | None
    problem: str | None
    test_sent: bool
    alerts_on: bool
    alerts_since: datetime | None
    phone_result: str | None  # "yes", "no" or None
    can_switch_on: bool
    push_on: bool = False  # the ntfy phone push is switched on (D38)
    push_topic: str | None = None  # shown to Ivan on his own page, to type into the ntfy app


def load_status(session: Session, profile: Profile, store: SecretStore) -> GmailStatus:
    address = (profile.gmail_address or "").strip() or None
    password_saved, store_error = False, None
    if address:
        try:
            password_saved = bool(store.get(address))
        except SecretStoreError as error:
            store_error = str(error)
    last_test = session.scalar(
        select(Event).where(Event.kind == "gmail_test_sent").order_by(Event.id.desc()).limit(1))
    test_sent = bool(address and last_test is not None
                     and (last_test.data or {}).get("to") == _tag(address))
    push_topic = None
    if profile.ntfy_enabled:
        try:
            stored = store.get(NTFY_ACCOUNT)
            push_topic = stored if valid_topic(stored) else None
        except SecretStoreError as error:
            store_error = store_error or str(error)
    return GmailStatus(
        push_on=bool(profile.ntfy_enabled), push_topic=push_topic,
        address=address, password_saved=password_saved, store_error=store_error,
        checked_at=profile.gmail_checked_at, problem=profile.gmail_problem,
        test_sent=test_sent, alerts_on=bool(profile.alerts_enabled),
        alerts_since=profile.alerts_enabled_at, phone_result=profile.phone_push_result,
        can_switch_on=bool(address and password_saved and test_sent),
    )


def save_credentials(session: Session, profile: Profile, store: SecretStore, *, address: str,
                     raw_password: str, smtp_factory: SmtpFactory = smtplib.SMTP,
                     now: datetime | None = None) -> dict[str, str]:
    """Check the address and password with a real login, and only then keep the password.
    Returns {} on success, otherwise {field: message} and NOTHING is stored."""
    errors: dict[str, str] = {}
    address = (address or "").strip()
    if not valid_address(address):
        errors["address"] = "Upišite ispravnu Gmail adresu, npr. ime.prezime@gmail.com."
    password = clean_app_password(raw_password)
    if password is None:
        errors["password"] = ("Aplikacijska lozinka ima točno 16 slova (Google je pokaže u četiri "
                              "skupine po četiri). Prepišite je ili je zalijepite cijelu.")
    if errors:
        return errors
    sender = GmailSender(address=address, password=password, smtp_factory=smtp_factory)
    try:
        sender.check_login()
    except GmailError as error:
        errors["password" if error.code == "auth" else "general"] = str(error)
        return errors
    previous = (profile.gmail_address or "").strip()
    try:
        store.set(address, password)
        if previous and previous != address:
            store.delete(previous)
    except SecretStoreError as error:
        return {"general": str(error)}
    now = now or utcnow()
    profile.gmail_address = address
    profile.gmail_checked_at = now
    profile.gmail_problem = None
    if previous != address:  # a different address: the old test and alert switch do not carry over
        profile.alerts_enabled = False
        profile.alerts_enabled_at = None
        profile.phone_push_result = None
    session.add(Event(kind="gmail_saved"))
    session.flush()
    return {}


def send_test(session: Session, profile: Profile, store: SecretStore, *,
              smtp_factory: SmtpFactory = smtplib.SMTP, now: datetime | None = None) -> str | None:
    """Send the test message to Ivan's own address. Returns an error message or None."""
    sender = build_sender(profile, store, smtp_factory=smtp_factory)
    if sender is None:
        return "Najprije spremite Gmail adresu i lozinku."
    try:
        sender.send_message(to=sender.address, subject=TEST_SUBJECT, text=TEST_TEXT)
    except GmailError as error:
        if error.code == "auth":
            profile.gmail_problem = str(error)[:300]
            profile.alerts_enabled = False
        session.flush()
        return str(error)
    profile.gmail_checked_at = now or utcnow()
    profile.gmail_problem = None
    session.add(Event(kind="gmail_test_sent", data={"to": _tag(sender.address)}))
    session.flush()
    return None


def answer_phone_test(session: Session, profile: Profile, heard: bool) -> None:
    profile.phone_push_result = "yes" if heard else "no"
    session.add(Event(kind="phone_push_answered", data={"heard": heard}))
    session.flush()


def set_alerts(session: Session, profile: Profile, store: SecretStore, on: bool, *,
               now: datetime | None = None) -> str | None:
    """Switch alerts on (only after a test message went out) or off. Returns an error or None."""
    if on:
        if not load_status(session, profile, store).can_switch_on:
            return "Najprije spremite lozinku i pošaljite sebi probnu poruku."
        profile.alerts_enabled = True
        profile.alerts_enabled_at = now or utcnow()  # only ads that appear from now on
        profile.gmail_problem = None
        session.add(Event(kind="alerts_on"))
    else:
        profile.alerts_enabled = False
        session.add(Event(kind="alerts_off"))
    session.flush()
    return None


def remove_credentials(session: Session, profile: Profile, store: SecretStore) -> str | None:
    """Delete the password from the store and switch alerts off. Returns an error or None."""
    address = (profile.gmail_address or "").strip()
    try:
        if address:
            store.delete(address)
    except SecretStoreError as error:
        return str(error)
    profile.alerts_enabled = False
    profile.gmail_checked_at = None
    profile.gmail_problem = None
    session.add(Event(kind="gmail_removed"))
    session.flush()
    return None


# ---------------------------------------------------------------- the phone push (D38)
PUSH_TEST_TITLE = "Probna obavijest"
PUSH_TEST_MESSAGE = ("Ako ovo vidite na mobitelu, obavijesti rade. Ovako će stizati novi poslovi.")


def enable_phone(session: Session, profile: Profile, store: SecretStore) -> str | None:
    """Switch the phone push on: make a secret topic (once) and keep it in the secret store."""
    try:
        topic = store.get(NTFY_ACCOUNT)
        if not valid_topic(topic):
            store.set(NTFY_ACCOUNT, new_topic())
    except SecretStoreError as error:
        return str(error)
    profile.ntfy_enabled = True
    session.add(Event(kind="ntfy_enabled"))
    session.flush()
    return None


def send_phone_test(session: Session, profile: Profile, store: SecretStore, *,
                    client: httpx.Client | None = None) -> str | None:
    """Send one test push. Returns an error message or None."""
    sender = build_ntfy_sender(store, client)
    if sender is None or not profile.ntfy_enabled:
        return "Najprije uključite obavijesti na mobitel."
    try:
        sender.publish(title=PUSH_TEST_TITLE, message=PUSH_TEST_MESSAGE, priority=4)
    except NtfyError as error:
        return str(error)
    session.add(Event(kind="ntfy_test_sent"))
    session.flush()
    return None


def disable_phone(session: Session, profile: Profile, store: SecretStore) -> str | None:
    """Switch the phone push off and delete the topic from the secret store."""
    try:
        store.delete(NTFY_ACCOUNT)
    except SecretStoreError as error:
        return str(error)
    profile.ntfy_enabled = False
    session.add(Event(kind="ntfy_disabled"))
    session.flush()
    return None
