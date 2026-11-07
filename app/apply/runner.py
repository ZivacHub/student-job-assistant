"""Sends the approved applications whose undo window is over (plan 5.2, D39).

Called by the scheduler once a minute (so an approved draft goes out also when no page is open)
and by the Prijave pages. All the rules live in `app/apply/gate.py`: this module only gives it the
Gmail sender and the CV folder. In trial mode (the default) nothing leaves the PC: due approvals
are only written to the log.
"""

import smtplib
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.apply import gate
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.models import Profile
from app.notify.gmail import GmailSender, build_sender
from app.notify.secrets import SecretStore


def sender_for(profile: Profile, store: SecretStore, *,
               smtp_factory=None) -> GmailSender | None:
    """The Gmail sender for Ivan's saved address, or None when Gmail is not connected. The mail
    connection can be replaced in tests (`smtp_factory`)."""
    return build_sender(profile, store, smtp_factory=smtp_factory or smtplib.SMTP)


def make_applier(session_factory: sessionmaker[Session], *,
                 store_getter: Callable[[], SecretStore],
                 cv_dir_getter: Callable[[], Path]) -> Callable[[], list[gate.Outcome]]:
    """What the scheduler calls: one round with its own database session."""

    def run() -> list[gate.Outcome]:
        with session_factory() as session:
            profile = get_or_create_profile(session)
            sender = sender_for(profile, store_getter())
            outcomes = gate.process_due(session, profile, sender,
                                        dry_run=get_settings().dry_run, cv_dir=cv_dir_getter())
            session.commit()
            return outcomes

    return run
