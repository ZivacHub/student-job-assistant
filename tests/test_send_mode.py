"""Real sending (S3): the switch between trial mode and real sending. Off by default, needs a
connected Gmail and a ticked confirmation, can be forced off by JOBS_DRY_RUN=true, is remembered
and logged, and the pages say plainly which mode is on. Nothing real is contacted."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.apply import service
from app.core import mode
from app.core.db import get_session
from app.core.profile import get_or_create_profile
from app.core.settings import get_settings
from app.main import create_app
from app.models import Cv, Event, Source
from app.models.base import utcnow
from app.notify.secrets import get_secret_store
from tests.gmail_fakes import ADDRESS, PASSWORD, FakeMail
from tests.test_apply_gate import Recorder
from tests.test_apply_service import add_job

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture(autouse=True)
def trial_mode(monkeypatch):
    """Every test starts in trial mode, and the real setting is restored afterwards."""
    monkeypatch.setattr(get_settings(), "dry_run", True)
    monkeypatch.delenv("JOBS_DRY_RUN", raising=False)


@pytest.fixture
def profile(db):
    p = get_or_create_profile(db)
    p.full_name, p.notify_threshold = "Ivan Primjer", 60
    p.cvs.append(Cv(label="Hrvatski", language="hr", path="cvs/hr.pdf", is_default=True))
    db.flush()
    return p


def connect_gmail(db, profile):
    profile.gmail_address, profile.gmail_checked_at = ADDRESS, utcnow()
    get_secret_store().set(ADDRESS, PASSWORD)
    db.commit()


@pytest.fixture
def web(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    app.state.smtp_factory = FakeMail()
    return SimpleNamespace(app=app, client=TestClient(app, follow_redirects=False), db=db)


# ---------------------------------------------------------------- the rules of the switch
def test_real_sending_is_off_by_default_and_after_a_restart(db, profile):
    assert profile.live_sending is False
    assert mode.sync_from_profile(db) is True and get_settings().dry_run is True


def test_it_cannot_be_switched_on_before_gmail_is_connected(db, profile):
    assert "Gmail" in mode.set_live(db, profile, True)
    assert profile.live_sending is False and get_settings().dry_run is True


def test_switching_on_and_off_is_remembered_logged_and_takes_effect_at_once(db, profile):
    connect_gmail(db, profile)
    assert mode.set_live(db, profile, True) is None
    assert profile.live_sending is True and get_settings().dry_run is False
    assert mode.set_live(db, profile, False) is None
    assert profile.live_sending is False and get_settings().dry_run is True
    kinds = [e.kind for e in db.scalars(select(Event))]
    assert kinds == ["live_sending_on", "live_sending_off"]


def test_the_choice_comes_back_when_the_app_starts(db, profile):
    connect_gmail(db, profile)
    profile.live_sending = True
    assert mode.sync_from_profile(db) is False and get_settings().dry_run is False
    profile.live_sending = False
    assert mode.sync_from_profile(db) is True and get_settings().dry_run is True


def test_the_setting_json_dry_run_true_forces_trial_mode_whatever_was_chosen(
        db, profile, monkeypatch):
    connect_gmail(db, profile)
    profile.live_sending = True
    monkeypatch.setenv("JOBS_DRY_RUN", "true")
    assert mode.sync_from_profile(db) is True  # the stored choice is ignored
    assert "JOBS_DRY_RUN" in mode.set_live(db, profile, True)
    assert get_settings().dry_run is True


def test_switching_back_to_trial_mode_is_always_allowed(db, profile):
    assert mode.set_live(db, profile, False) is None and get_settings().dry_run is True


# ---------------------------------------------------------------- the Prijave page
def test_the_page_says_trial_mode_is_on_and_offers_the_switch_only_with_gmail(web, profile):
    body = web.client.get("/prijave").text
    assert "Probni način" in body and "<strong>Uključen.</strong>" in body
    assert "Slanje je isključeno" in body  # the bar at the top
    assert "Uključi stvarno slanje" not in body and "tek kad je Gmail povezan" in body
    connect_gmail(web.db, profile)
    body = web.client.get("/prijave").text
    assert "Uključi stvarno slanje" in body and 'name="potvrda"' in body
    assert "Ništa ne odlazi bez mog odobrenja tog točnog teksta" in body


def test_switching_on_needs_the_ticked_confirmation(web, profile):
    connect_gmail(web.db, profile)
    r = web.client.post("/prijave/stvarno-slanje", headers=ORIGIN, data={"stanje": "on"})
    assert r.status_code == 200 and "Označite potvrdu" in r.text and "data-error-summary" in r.text
    assert get_settings().dry_run is True and get_or_create_profile(web.db).live_sending is False


def test_the_confirmed_switch_turns_real_sending_on_and_every_page_says_so(web, profile):
    connect_gmail(web.db, profile)
    r = web.client.post("/prijave/stvarno-slanje", headers=ORIGIN,
                        data={"stanje": "on", "potvrda": "da"})
    assert r.status_code == 303 and r.headers["location"] == "/prijave?ok=stvarno_on#stanje"
    assert get_settings().dry_run is False
    page = web.client.get("/prijave?ok=stvarno_on")
    assert "Stvarno slanje je uključeno" in page.text and "Vrati u probni način" in page.text
    assert "Uključi stvarno slanje" not in page.text
    assert "Poruke odobrene u probnom načinu treba odobriti ponovno" in page.text
    assert 'class="mode mode--live"' in web.client.get("/poslovi").text
    assert "Slanje je isključeno" not in web.client.get("/gmail").text


def test_switching_back_returns_to_trial_mode(web, profile):
    connect_gmail(web.db, profile)
    web.client.post("/prijave/stvarno-slanje", headers=ORIGIN,
                    data={"stanje": "on", "potvrda": "da"})
    r = web.client.post("/prijave/stvarno-slanje", headers=ORIGIN, data={"stanje": "off"})
    assert r.status_code == 303 and get_settings().dry_run is True
    assert "Vraćeno je u probni način" in web.client.get("/prijave?ok=stvarno_off").text


def test_switching_on_without_gmail_is_refused_with_a_plain_message(web):
    r = web.client.post("/prijave/stvarno-slanje", headers=ORIGIN,
                        data={"stanje": "on", "potvrda": "da"})
    assert r.status_code == 200 and "Najprije povežite Gmail" in r.text
    assert get_settings().dry_run is True


def test_when_the_setting_forces_trial_mode_the_page_says_so_and_refuses(web, profile, monkeypatch):
    connect_gmail(web.db, profile)
    monkeypatch.setenv("JOBS_DRY_RUN", "true")
    assert "Prisilno uključen u postavkama programa" in web.client.get("/prijave").text
    r = web.client.post("/prijave/stvarno-slanje", headers=ORIGIN,
                        data={"stanje": "on", "potvrda": "da"})
    assert r.status_code == 200 and "JOBS_DRY_RUN" in r.text and get_settings().dry_run is True


@pytest.mark.parametrize("headers", [{}, {"origin": "http://evil.example"}])
def test_posts_from_another_origin_never_switch_anything(web, profile, headers):
    connect_gmail(web.db, profile)
    r = web.client.post("/prijave/stvarno-slanje", headers=headers,
                        data={"stanje": "on", "potvrda": "da"})
    assert r.status_code in (400, 403) and get_settings().dry_run is True


def test_the_log_says_in_plain_croatian_when_real_sending_was_switched(web, profile):
    connect_gmail(web.db, profile)
    web.client.post("/prijave/stvarno-slanje", headers=ORIGIN,
                    data={"stanje": "on", "potvrda": "da"})
    web.client.post("/prijave/stvarno-slanje", headers=ORIGIN, data={"stanje": "off"})
    body = web.client.get("/dnevnik").text
    assert "Vi ste uključili stvarno slanje" in body and "Vi ste vratili program u probni" in body


# ------------------------------------------------------- the page really sends in live mode
def approved_draft(db, profile, n=1):
    source = db.query(Source).first() or Source(key="sp", name="studentski-poslovi.hr")
    db.add(source)
    db.flush()
    now = utcnow()
    job = add_job(db, source, n)
    job.first_seen_at = now - timedelta(hours=1)
    application = service.ensure_draft(db, job, profile, now)
    approved_at = now - timedelta(minutes=5)
    assert service.approve(db, application, profile, approved_at, dry_run=False) == {}
    application.send_after = now - timedelta(minutes=1)
    db.commit()
    return application


def test_opening_the_page_sends_a_due_approval_once_through_the_saved_gmail(
        web, profile, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF-1.4 fake")
    connect_gmail(web.db, profile)
    web.client.post("/prijave/stvarno-slanje", headers=ORIGIN,
                    data={"stanje": "on", "potvrda": "da"})
    application = approved_draft(web.db, profile)
    for _ in range(3):  # the page tick runs on every visit: still one message
        web.client.get("/prijave")
    assert [m["To"] for m in web.app.state.smtp_factory.sent] == ["posao@example.hr"]
    assert web.db.get(type(application), application.id).state == "sent"


def test_a_pretend_sender_on_the_app_is_used_instead_of_gmail(web, profile, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "hr.pdf").write_bytes(b"%PDF")
    connect_gmail(web.db, profile)
    web.client.post("/prijave/stvarno-slanje", headers=ORIGIN,
                    data={"stanje": "on", "potvrda": "da"})
    web.app.state.sender = Recorder()
    approved_draft(web.db, profile)
    web.client.get("/prijave")
    assert len(web.app.state.sender.calls) == 1 and web.app.state.smtp_factory.sent == []
