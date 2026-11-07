"""Trial mode or real sending: the one switch that decides whether an approved application leaves
the PC (plan 5.2 + 5.4, D39).

- Real sending is OFF by default. Only Ivan turns it on, on the Prijave page, with a confirmation;
  nothing else (no script, no helper, no setting file) switches it on. The choice is remembered in
  the profile (`live_sending`) and restored when the app starts.
- `JOBS_DRY_RUN=true` in the environment forces trial mode: the page switch then refuses.
- The running value lives in `Settings.dry_run` (True = trial mode), which the rest of the app
  already reads.
"""

import os

from sqlalchemy.orm import Session

from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.models import Event, Profile


def env_forces_dry_run() -> bool:
    return os.environ.get("JOBS_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def sync_from_profile(session: Session) -> bool:
    """At startup: restore the choice Ivan made last time. Returns the trial-mode value."""
    settings = get_settings()
    profile = get_or_create_profile(session)
    settings.dry_run = True if env_forces_dry_run() else not profile.live_sending
    return settings.dry_run


def gmail_connected(profile: Profile) -> bool:
    return bool(profile.gmail_address and profile.gmail_checked_at)


def set_live(session: Session, profile: Profile, live: bool) -> str | None:
    """Switch real sending on or off. Returns an error message (plain Croatian) or None."""
    settings = get_settings()
    if live:
        if env_forces_dry_run():
            return ("Probni način je prisilno uključen u postavkama programa (JOBS_DRY_RUN=true), "
                    "pa se stvarno slanje ne može uključiti ovdje.")
        if not gmail_connected(profile):
            return "Najprije povežite Gmail na stranici Gmail."
    profile.live_sending = bool(live)
    settings.dry_run = not live
    session.add(Event(kind="live_sending_on" if live else "live_sending_off"))
    session.flush()
    return None
