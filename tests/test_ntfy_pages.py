"""ntfy phone push (N3): the phone section of the Gmail page. Switching on makes a secret
topic kept only in the secret store and shows it; test push; switching off deletes it. Nothing
real is called."""

import json
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import get_session
from app.core.profile import get_or_create_profile
from app.main import create_app
from app.models import Event, Profile
from app.notify.ntfy import NTFY_ACCOUNT, valid_topic
from app.notify.secrets import get_secret_store
from tests.gmail_fakes import FakeMail
from tests.test_event_log import log_event, make_source, texts

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def web(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    app.state.smtp_factory = FakeMail()
    seen = []
    state = {"status": 200}

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(state["status"])

    app.state.ntfy_client = httpx.Client(transport=httpx.MockTransport(handler))
    return SimpleNamespace(app=app, client=TestClient(app, follow_redirects=False), db=db,
                           pushes=seen, ntfy=state)


def enable(web):
    return web.client.post("/gmail/mobitel", headers=ORIGIN, data={"stanje": "on"})


def topic_in_page(page):
    found = re.search(r'<code class="topic">([^<]+)</code>', page.text)
    return found.group(1) if found else None


# ---------------------------------------------------------------- before switching on
def test_the_page_explains_what_leaves_the_pc_before_the_phone_push_is_switched_on(web):
    body = web.client.get("/gmail").text
    assert "4. Obavijest na mobitel (ntfy)" in body and "Uključi obavijesti na mobitel" in body
    assert "Nikad broj telefona, opis oglasa, nacrt poruke, Vaše ime ni lozinka" in body
    assert "Windows Credential Manager" in body and "<code" not in body
    assert "Pošalji probnu obavijest na mobitel" not in body


# ---------------------------------------------------------------- switching on
def test_switching_on_makes_a_secret_topic_stored_only_in_the_secret_store_and_shows_it(web):
    r = enable(web)
    assert r.status_code == 303 and r.headers["location"] == "/gmail?ok=mobitel_on#mobitel"
    topic = get_secret_store().get(NTFY_ACCOUNT)
    assert valid_topic(topic) and topic.startswith("sja-")
    page = web.client.get("/gmail?ok=mobitel_on")
    assert "Obavijesti na mobitel su uključene" in page.text
    assert topic_in_page(page) == topic
    assert "Google Play" in page.text and "tajan kao lozinka" in page.text
    assert get_or_create_profile(web.db).ntfy_enabled is True


def test_the_topic_is_nowhere_in_the_database_the_address_or_the_log(web, caplog):
    caplog.set_level("DEBUG")
    r = enable(web)
    topic = get_secret_store().get(NTFY_ACCOUNT)
    web.client.post("/gmail/mobitel/proba", headers=ORIGIN)
    web.db.commit()
    assert topic not in r.headers["location"] and topic not in caplog.text
    everything = repr([(c.name, getattr(get_or_create_profile(web.db), c.name))
                       for c in Profile.__table__.columns])
    everything += repr([(e.kind, e.data) for e in web.db.scalars(select(Event))])
    assert topic not in everything


def test_switching_on_twice_keeps_the_same_topic(web):
    enable(web)
    first = get_secret_store().get(NTFY_ACCOUNT)
    enable(web)
    assert get_secret_store().get(NTFY_ACCOUNT) == first


def test_a_stored_topic_that_is_not_valid_is_not_shown_and_the_page_says_what_to_do(web):
    enable(web)
    get_secret_store().set(NTFY_ACCOUNT, "<script>window.__x=1</script>")
    body = web.client.get("/gmail").text
    assert "<script>window.__x" not in body and "Tema nije dostupna" in body


# ---------------------------------------------------------------- the test push
def test_the_test_push_goes_to_ntfy_with_the_topic_and_says_so(web):
    enable(web)
    topic = get_secret_store().get(NTFY_ACCOUNT)
    r = web.client.post("/gmail/mobitel/proba", headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/gmail?ok=mobitel_proba#mobitel"
    assert len(web.pushes) == 1 and web.pushes[0]["topic"] == topic
    assert web.pushes[0]["title"] == "Probna obavijest" and web.pushes[0]["priority"] == 4
    assert "Probna obavijest je poslana" in web.client.get("/gmail?ok=mobitel_proba").text
    assert "ntfy_test_sent" in [e.kind for e in web.db.scalars(select(Event))]


def test_a_test_push_before_switching_on_says_what_to_do_first(web):
    r = web.client.post("/gmail/mobitel/proba", headers=ORIGIN)
    assert r.status_code == 200 and "Najprije uključite obavijesti na mobitel" in r.text
    assert web.pushes == []


@pytest.mark.parametrize("status, words", [(500, "Nije moguće spojiti se na ntfy"),
                                          (429, "ntfy je odbio poruku")])
def test_a_failing_ntfy_is_reported_in_plain_words(web, status, words):
    enable(web)
    web.ntfy["status"] = status
    r = web.client.post("/gmail/mobitel/proba", headers=ORIGIN)
    assert r.status_code == 200 and words in r.text and "data-error-summary" in r.text
    error_box = re.search(r'<p class="alert alert--error"[^>]*>(.*?)</p>', r.text, re.S).group(1)
    assert get_secret_store().get(NTFY_ACCOUNT) not in error_box  # the topic is not in the error


# ---------------------------------------------------------------- switching off
def test_switching_off_deletes_the_topic_and_brings_back_the_switch_on_button(web):
    enable(web)
    r = web.client.post("/gmail/mobitel", headers=ORIGIN, data={"stanje": "off"})
    assert r.status_code == 303
    assert get_secret_store().get(NTFY_ACCOUNT) is None
    assert get_or_create_profile(web.db).ntfy_enabled is False
    body = web.client.get("/gmail?ok=mobitel_off").text
    assert "tema je izbrisana" in body and "Uključi obavijesti na mobitel" in body
    assert "<code" not in body
    kinds = [e.kind for e in web.db.scalars(select(Event))]
    assert "ntfy_enabled" in kinds and "ntfy_disabled" in kinds


def test_a_new_topic_after_switching_off_and_on_is_a_different_one(web):
    enable(web)
    old = get_secret_store().get(NTFY_ACCOUNT)
    web.client.post("/gmail/mobitel", headers=ORIGIN, data={"stanje": "off"})
    enable(web)
    assert get_secret_store().get(NTFY_ACCOUNT) != old


# ---------------------------------------------------------------- safety and the log
@pytest.mark.parametrize("path, data", [("/gmail/mobitel", {"stanje": "on"}),
                                        ("/gmail/mobitel/proba", {})])
def test_posts_from_another_origin_are_refused_and_change_nothing(web, path, data):
    for headers in ({}, {"origin": "http://evil.example"}):
        assert web.client.post(path, headers=headers, data=data).status_code in (400, 403)
    assert get_secret_store().get(NTFY_ACCOUNT) is None
    assert get_or_create_profile(web.db).ntfy_enabled is False


def test_the_log_says_what_happened_in_plain_croatian(db):
    src = make_source(db)
    for minute, kind in enumerate(["ntfy_enabled", "ntfy_test_sent", "push_failed",
                                   "ntfy_disabled"], start=1):
        log_event(db, kind, None, minute)
    lines = texts(db)
    assert len(lines) == 4 and not any(line.startswith("Događaj:") for line in lines)
    assert "Vi ste uključili obavijesti na mobitel (ntfy)." in lines
    assert any("E-mail je poslan normalno" in line for line in lines)
    assert src is not None
