"""A-15: a migration that rebuilds a table (SQLite batch mode) must not delete child rows."""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.db import make_engine
from tests.conftest import ROOT

BEFORE_APPROVAL_GATE = "dcb809ebddad"  # profile still has auto_send_threshold, application.auto


def cfg_for(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_cvs_and_applications_survive_the_approval_gate_migration(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, BEFORE_APPROVAL_GATE)

    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO profile (id, full_name, has_car, categories_wanted, categories_excluded,"
            " weights, tone, notify_threshold, auto_send_threshold) "
            "VALUES (1, 'T', 1, '[]', '[]', '{}', 'Vi', 60, 80)"
        ))
        c.execute(text(
            "INSERT INTO cv (id, profile_id, label, language, path, rule, is_default) "
            "VALUES (1, 1, 'Zivotopis', 'hr', 'data/cvs/hr.pdf', '{}', 1)"
        ))
        c.execute(text("INSERT INTO source (id, key, name, enabled, interval_minutes,"
                       " consecutive_failures, last_items) VALUES (1, 's', 's', 1, 10, 0, 0)"))
        c.execute(text(
            "INSERT INTO job (id, source_id, external_id, url, title, status, channel,"
            " first_seen_at)"
            " VALUES (1, 1, 'e1', 'https://x', 'Konobar', 'new', 'email', '2026-10-09 00:00:00')"
        ))
        c.execute(text(
            "INSERT INTO application (id, job_id, state, cv_id, auto, dry_run, created_at) "
            "VALUES (1, 1, 'drafted', 1, 0, 1, '2026-10-09 00:00:00')"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        counts = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                  for t in ("profile", "cv", "source", "job", "application")}
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
        digest = tuple(c.execute(
            text("SELECT digest_threshold, digest_interval_hours FROM profile")).one())
    engine.dispose()
    assert counts == {"profile": 1, "cv": 1, "source": 1, "job": 1, "application": 1}
    assert fk_errors == []
    assert digest == (40, 3)  # new columns got their defaults on the existing profile row


BEFORE_SNOOZED_UNTIL = "d8bcc9f1a16f"


def test_jobs_survive_the_snoozed_until_migration(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, BEFORE_SNOOZED_UNTIL)

    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO source (id, key, name, enabled, interval_minutes,"
                       " consecutive_failures, last_items) VALUES (1, 's', 's', 1, 10, 0, 0)"))
        for job_id, status in ((1, "new"), (2, "snoozed")):
            c.execute(text(
                "INSERT INTO job (id, source_id, external_id, url, title, status, channel,"
                " first_seen_at) VALUES (:i, 1, :e, 'https://x', 'Konobar', :s, 'email',"
                " '2026-10-09 00:00:00')"), {"i": job_id, "e": f"e{job_id}", "s": status})
        c.execute(text(
            "INSERT INTO event (kind, job_id, ts) VALUES ('collected', 1, '2026-10-09')"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        rows = c.execute(text("SELECT id, status, snoozed_until FROM job ORDER BY id")).all()
        events = c.execute(text("SELECT COUNT(*) FROM event")).scalar()
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
    engine.dispose()
    assert [tuple(r) for r in rows] == [(1, "new", None), (2, "snoozed", None)]
    assert events == 1 and fk_errors == []



BEFORE_REAL_SENDING = "e6a1b3c9d742"  # no live switch, no claim time yet
BEFORE_NTFY = "c4d8e1f7a935"  # profile has no ntfy switch yet
BEFORE_GMAIL_SETUP = "a7c91e45d2b8"  # profile has no gmail columns yet
BEFORE_APPLY_SAFETY = "b3f0c6a7d2e1"


def test_profiles_and_applications_survive_the_apply_safety_migration(tmp_path):
    """Phase 5: the safety numbers, the kill switch and the approval fingerprint are new columns;
    existing rows keep their data and get the defaults of D34 (10 a day, 14 days, 2 minutes)."""
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, BEFORE_APPLY_SAFETY)

    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO profile (id, full_name, has_car, categories_wanted, categories_excluded,"
            " weights, tone, notify_threshold, digest_threshold, digest_interval_hours) "
            "VALUES (1, 'T', 0, '[]', '[]', '{}', 'Vi', 60, 40, 3)"))
        c.execute(text("INSERT INTO source (id, key, name, enabled, interval_minutes,"
                       " consecutive_failures, last_items) VALUES (1, 's', 's', 1, 10, 0, 0)"))
        c.execute(text(
            "INSERT INTO job (id, source_id, external_id, url, title, status, channel,"
            " first_seen_at) VALUES (1, 1, 'e1', 'https://x', 'Konobar', 'new', 'email',"
            " '2026-10-09 00:00:00')"))
        c.execute(text(
            "INSERT INTO application (id, job_id, state, to_email, subject, body, dry_run,"
            " approved_at, created_at) VALUES (1, 1, 'approved', 'a@example.hr', 'S', 'B', 1,"
            " '2026-10-18 10:00:00', '2026-10-18 09:00:00')"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        profile = tuple(c.execute(text(
            "SELECT full_name, apply_daily_cap, apply_cooldown_days, apply_undo_minutes,"
            " sending_paused FROM profile")).one())
        application = tuple(c.execute(text(
            "SELECT state, to_email, approved_at, approved_hash, dry_run_at"
            " FROM application")).one())
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
    engine.dispose()
    assert profile == ("T", 10, 14, 2, 0)
    assert application[:2] == ("approved", "a@example.hr") and application[2] is not None
    assert application[3] is None and application[4] is None  # no fingerprint: never sendable
    assert fk_errors == []


def test_profiles_survive_the_gmail_setup_migration_with_alerts_off(tmp_path):
    """Gmail round 1: the address, the alert switch and the phone test are new profile columns;
    an existing profile keeps its data and starts with alerts OFF and nothing saved."""
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, BEFORE_GMAIL_SETUP)

    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO profile (id, full_name, has_car, categories_wanted, categories_excluded,"
            " weights, tone, notify_threshold, digest_threshold, digest_interval_hours) "
            "VALUES (1, 'T', 1, '[]', '[]', '{}', 'Vi', 70, 40, 3)"))
        c.execute(text(
            "INSERT INTO cv (id, profile_id, label, language, path, rule, is_default) "
            "VALUES (1, 1, 'Zivotopis', 'hr', 'data/cvs/hr.pdf', '{}', 1)"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        profile = tuple(c.execute(text(
            "SELECT full_name, has_car, notify_threshold, gmail_address, gmail_checked_at,"
            " gmail_problem, alerts_enabled, alerts_enabled_at, phone_push_result"
            " FROM profile")).one())
        cvs = c.execute(text("SELECT label FROM cv")).fetchall()
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
    engine.dispose()
    assert profile == ("T", 1, 70, None, None, None, 0, None, None)
    assert [row[0] for row in cvs] == ["Zivotopis"] and fk_errors == []


def test_profiles_survive_the_ntfy_migration_with_the_phone_push_off(tmp_path):
    """The ntfy phone push adds one profile switch; an existing profile keeps everything and the
    switch starts OFF."""
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, BEFORE_NTFY)

    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO profile (id, full_name, has_car, categories_wanted, categories_excluded,"
            " weights, tone, notify_threshold, digest_threshold, digest_interval_hours,"
            " gmail_address, alerts_enabled) "
            "VALUES (1, 'T', 1, '[]', '[]', '{}', 'Vi', 70, 40, 3, 'a@gmail.com', 1)"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        row = tuple(c.execute(text(
            "SELECT full_name, gmail_address, alerts_enabled, ntfy_enabled FROM profile")).one())
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
    engine.dispose()
    assert row == ("T", "a@gmail.com", 1, 0) and fk_errors == []


def test_profiles_and_applications_survive_the_real_sending_migration_in_trial_mode(tmp_path):
    """Real sending adds a profile switch and an application claim time. Existing rows keep
    everything; the switch starts OFF (trial mode) and an approved application is unchanged."""
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, BEFORE_REAL_SENDING)

    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO profile (id, full_name, has_car, categories_wanted, categories_excluded,"
            " weights, tone, notify_threshold, digest_threshold, digest_interval_hours,"
            " ntfy_enabled) VALUES (1, 'T', 0, '[]', '[]', '{}', 'Vi', 60, 40, 3, 1)"))
        c.execute(text("INSERT INTO source (id, key, name, enabled, interval_minutes,"
                       " consecutive_failures, last_items) VALUES (1, 's', 's', 1, 10, 0, 0)"))
        c.execute(text(
            "INSERT INTO job (id, source_id, external_id, url, title, status, channel,"
            " first_seen_at) VALUES (1, 1, 'e1', 'https://x', 'Konobar', 'new', 'email',"
            " '2026-10-09 00:00:00')"))
        c.execute(text(
            "INSERT INTO application (id, job_id, state, to_email, subject, body, dry_run,"
            " approved_at, created_at) VALUES (1, 1, 'approved', 'a@example.hr', 'S', 'B', 1,"
            " '2026-10-18 10:00:00', '2026-10-18 09:00:00')"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        profile = tuple(c.execute(text(
            "SELECT full_name, ntfy_enabled, live_sending FROM profile")).one())
        application = tuple(c.execute(text(
            "SELECT state, to_email, sending_at FROM application")).one())
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
    engine.dispose()
    assert profile == ("T", 1, 0)  # real sending is OFF after the upgrade
    assert application == ("approved", "a@example.hr", None) and fk_errors == []
