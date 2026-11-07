"""Gmail round 1 (Q4): the Gmail page: steps, password box, test message, phone test, alert switch,
remove password. It never echoes the password, refuses posts from other origins, escapes text."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import get_session
from app.core.profile import get_or_create_profile
from app.main import create_app
from app.models import Event
from app.notify.secrets import MemoryStore, get_secret_store
from tests.gmail_fakes import ADDRESS, PASSWORD, PASSWORD_SHOWN, FakeMail, auth_error

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def web(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    app.state.smtp_factory = FakeMail()
    return SimpleNamespace(app=app, client=TestClient(app, follow_redirects=False), db=db)


def connect(web, mail=None, address=ADDRESS, password=PASSWORD_SHOWN):
    if mail is not None:
        web.app.state.smtp_factory = mail
    return web.client.post("/gmail/spremi", headers=ORIGIN,
                           data={"address": address, "password": password})


def text(response):
    return response.text


# ---------------------------------------------------------------- the empty page
def test_the_page_explains_the_steps_and_the_risk_before_anything_is_saved(web):
    r = web.client.get("/gmail")
    body = text(r)
    assert r.status_code == 200 and "<h1>Gmail</h1>" in body
    assert "potvrdu u dva koraka" in body and "myaccount.google.com/apppasswords" in body
    assert "Windows Credential Manager" in body and "čita poštu" in body  # the plain-words risk
    assert 'type="password"' in body and 'autocomplete="new-password"' in body
    assert "Nije spremljena." in body and "Nije postavljena." in body
    assert 'rel="noopener noreferrer"' in body
    assert "Pošalji probnu poruku" not in body
    assert 'action="/gmail/obavijesti"' not in body  # the Gmail alert switch needs a test first


def test_the_menu_has_a_gmail_entry_and_marks_it_current(web):
    body = text(web.client.get("/gmail"))
    assert '<a href="/gmail" aria-current="page">Gmail</a>' in body
    assert '<a href="/gmail" >Gmail</a>' in text(web.client.get("/poslovi"))


# ---------------------------------------------------------------- saving the password
def test_a_good_password_connects_gmail_and_the_page_never_shows_it(web):
    r = connect(web)
    assert r.status_code == 303 and r.headers["location"] == "/gmail?ok=spremljeno"
    page = web.client.get("/gmail?ok=spremljeno")
    body = text(page)
    assert "Gmail je povezan" in body and ADDRESS in body
    assert PASSWORD not in body and PASSWORD_SHOWN not in body
    assert get_secret_store().get(ADDRESS) == PASSWORD
    assert "Pošalji probnu poruku sebi" in body


def test_a_refused_password_shows_a_plain_error_and_never_echoes_it(web):
    r = connect(web, FakeMail(login_error=auth_error()))
    body = text(r)
    assert r.status_code == 200 and "Gmail nije povezan" in body and "16 slova" in body
    assert 'aria-invalid="true"' in body and "data-error-summary" in body
    assert f'value="{ADDRESS}"' in body
    assert PASSWORD not in body and PASSWORD_SHOWN not in body
    assert get_secret_store().get(ADDRESS) is None


def test_a_wrong_address_or_a_short_password_is_refused_before_any_connection(web):
    mail = FakeMail()
    body = text(connect(web, mail, address="not an address", password="abc"))
    assert "Upišite ispravnu Gmail adresu" in body and "16 slova" in body
    assert mail.connects == []


def test_hostile_text_in_the_address_field_is_escaped(web):
    body = text(connect(web, FakeMail(), address='"><script>window.__x=1</script>'))
    assert "<script>window.__x" not in body and "&lt;script&gt;" in body


def test_the_password_never_travels_in_an_address_or_a_log_line(web, caplog):
    caplog.set_level("DEBUG")
    r = connect(web)
    assert PASSWORD not in r.headers.get("location", "")
    assert PASSWORD not in caplog.text and PASSWORD_SHOWN not in caplog.text


# ---------------------------------------------------------------- test message and phone test
def test_the_test_message_goes_to_ivan_and_then_the_phone_question_appears(web):
    connect(web)
    mail = FakeMail()
    web.app.state.smtp_factory = mail
    r = web.client.post("/gmail/probna", headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/gmail?ok=probna#telefon"
    assert [m["To"] for m in mail.sent] == [ADDRESS]
    body = text(web.client.get("/gmail?ok=probna"))
    assert "Probna poruka je poslana" in body and "Je li mobitel zazvonio" in body
    assert "Uključi obavijesti" in body  # now allowed


def test_a_failed_test_message_says_why(web):
    connect(web)
    web.app.state.smtp_factory = FakeMail(connect_error=OSError("offline"))
    r = web.client.post("/gmail/probna", headers=ORIGIN)
    assert r.status_code == 200 and "Nije moguće spojiti se na Gmail" in text(r)


def test_the_phone_answer_is_kept_and_a_no_points_to_the_free_fallback(web):
    connect(web)
    web.client.post("/gmail/probna", headers=ORIGIN)
    no = web.client.post("/gmail/telefon", headers=ORIGIN, data={"odgovor": "ne"})
    assert no.status_code == 303
    body = text(web.client.get("/gmail"))
    assert "Zato je u koraku 4 ispod besplatna zamjena" in body and "Mobitel zazvonio" in body
    assert get_or_create_profile(web.db).phone_push_result == "no"
    web.client.post("/gmail/telefon", headers=ORIGIN, data={"odgovor": "da"})
    assert get_or_create_profile(web.db).phone_push_result == "yes"
    assert "Zato je u koraku 4 ispod besplatna zamjena" not in text(web.client.get("/gmail"))


def test_a_phone_answer_that_is_neither_yes_nor_no_is_refused(web):
    r = web.client.post("/gmail/telefon", headers=ORIGIN, data={"odgovor": "možda"})
    assert r.status_code == 200 and "Odaberite jedan od dva odgovora" in text(r)


# ---------------------------------------------------------------- the alert switch
def test_alerts_cannot_be_switched_on_before_a_test_message(web):
    connect(web)
    r = web.client.post("/gmail/obavijesti", headers=ORIGIN, data={"stanje": "on"})
    assert r.status_code == 200 and "probnu poruku" in text(r)
    assert get_or_create_profile(web.db).alerts_enabled is False


def test_alerts_can_be_switched_on_and_off_and_each_leaves_a_log_line(web):
    connect(web)
    web.client.post("/gmail/probna", headers=ORIGIN)
    on = web.client.post("/gmail/obavijesti", headers=ORIGIN, data={"stanje": "on"})
    assert on.status_code == 303
    profile = get_or_create_profile(web.db)
    assert profile.alerts_enabled is True and profile.alerts_enabled_at is not None
    body = text(web.client.get("/gmail?ok=alarmi_on"))
    assert "Uključene" in body and "Stizat će samo za oglase koji se pojave od sada" in body
    assert f"ocjenom {profile.notify_threshold} ili višom" in body
    web.client.post("/gmail/obavijesti", headers=ORIGIN, data={"stanje": "off"})
    assert get_or_create_profile(web.db).alerts_enabled is False
    kinds = [e.kind for e in web.db.scalars(select(Event))]
    assert "alerts_on" in kinds and "alerts_off" in kinds


def test_a_refused_password_is_shown_on_the_page_and_alerts_stay_off(web):
    connect(web)
    web.client.post("/gmail/probna", headers=ORIGIN)
    web.client.post("/gmail/obavijesti", headers=ORIGIN, data={"stanje": "on"})
    profile = get_or_create_profile(web.db)
    profile.alerts_enabled = False
    profile.gmail_problem = "Gmail je odbio lozinku. Provjerite ..."
    web.db.commit()
    body = text(web.client.get("/gmail"))
    assert "Gmail je odbio lozinku" in body and "Obavijesti su zato isključene" in body


# ---------------------------------------------------------------- removing, safety
def test_removing_the_password_deletes_it_and_hides_the_button(web):
    connect(web)
    assert "Ukloni lozinku" in text(web.client.get("/gmail"))
    r = web.client.post("/gmail/ukloni", headers=ORIGIN)
    assert r.status_code == 303
    assert get_secret_store().get(ADDRESS) is None
    body = text(web.client.get("/gmail?ok=uklonjeno"))
    assert "Lozinka je uklonjena" in body and "Ukloni lozinku" not in body


@pytest.mark.parametrize("path, data", [
    ("/gmail/spremi", {"address": ADDRESS, "password": PASSWORD}), ("/gmail/probna", {}),
    ("/gmail/telefon", {"odgovor": "da"}), ("/gmail/obavijesti", {"stanje": "on"}),
    ("/gmail/ukloni", {})])
def test_every_post_from_another_origin_is_refused_and_changes_nothing(web, path, data):
    for headers in ({}, {"origin": "http://evil.example"}):
        r = web.client.post(path, headers=headers, data=data)
        assert r.status_code in (400, 403)
    assert get_secret_store().get(ADDRESS) is None
    assert get_or_create_profile(web.db).gmail_address is None


def test_the_prijave_page_tells_the_truth_about_gmail_and_sending(web):
    assert "Nije postavljeno" in text(web.client.get("/prijave"))
    connect(web)
    body = text(web.client.get("/prijave"))
    assert "Postavljen. Poruke poslodavcima odlaze samo kad je uključeno stvarno slanje" in body
    assert "Uključi stvarno slanje" in body  # the switch is offered once Gmail is connected


def test_the_default_store_in_tests_is_memory_only(web):
    assert isinstance(get_secret_store(), MemoryStore)
