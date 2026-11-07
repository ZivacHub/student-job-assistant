"""Step 2.7 / 4.4: the Dodaj oglas form (paste in an ad, check what was understood, save)."""

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.paste_form import (
    MAX_CHARS,
    PasteValues,
    build_preview,
    clean_text,
    format_pay,
    parse_first_step,
    parse_pay_field,
    parse_second_step,
)
from app.main import create_app
from app.models import Job, Place, Source

ORIGIN = {"origin": "http://testserver"}
FACEBOOK_AD = (
    "🔥 Traži se konobar/ica za vikend u kafiću u Osijeku. Plaća 7 €/h. "
    "Javiti se na 091 234 5678 ili poslati CV na kafic@example.hr. Caffe Bar Luna d.o.o."
)


# ---------------------------------------------------------------- the form values
def test_text_is_tidied():
    assert clean_text("  a\r\nb\rc  ") == "a\nb\nc"
    assert clean_text(None) == ""


@pytest.mark.parametrize("raw, value", [
    ("7,50", 7.5), ("7.5", 7.5), (" € 8 ", 8.0), ("12", 12.0), ("", None), ("   ", None),
])
def test_pay_is_read_with_a_comma_or_a_dot(raw, value):
    assert parse_pay_field(raw) == (value, None)


@pytest.mark.parametrize("raw", ["abc", "7 eura", "0", "-3", "101", "1e3", "nan"])
def test_a_bad_pay_is_explained(raw):
    value, error = parse_pay_field(raw)
    assert value is None and error.startswith("Plaća:")


def test_pay_is_shown_with_a_comma():
    assert format_pay(7.0) == "7,00" and format_pay(None) == ""


def test_step_one_needs_text_and_an_http_link_if_any():
    values, errors = parse_first_step({"text": "  ", "url": "ftp://x.hr/a"})
    assert set(errors) == {"text", "url"} and values.url == ""
    values, errors = parse_first_step(
        {"text": "Posao\r\nu Osijeku", "url": " https://fb.example/p/1 "})
    assert errors == {} and values.text == "Posao\nu Osijeku" and values.url == "https://fb.example/p/1"
    for bad in ("javascript:alert(1)", "data:text/html,x", "//x.hr/a", "www.x.hr", "a" * 1001):
        assert "url" in parse_first_step({"text": "x", "url": bad})[1]
    assert parse_first_step({"text": "x", "url": ""})[1] == {}


def test_preview_guesses_like_the_parser_and_lists_contacts():
    preview = build_preview(PasteValues(text=FACEBOOK_AD))
    v = preview.values
    assert v.title.startswith("Traži se konobar/ica za vikend u kafiću u Osijeku")
    assert (v.employer, v.city, v.pay) == ("Caffe Bar Luna d.o.o.", "Osijek", "7,00")
    assert ("email", "kafic@example.hr") in preview.contacts
    assert ("phone", "091 234 5678") in preview.contacts  # as people read it, not +385...
    assert preview.channel == "email" and preview.channel_label == "E-mail"
    assert preview.shortened is False


def test_preview_without_contacts_or_link_says_there_is_no_way_to_apply():
    preview = build_preview(PasteValues(text="Treba nam pomoć u skladištu u Osijeku."))
    assert preview.contacts == [] and preview.channel == "none"
    assert preview.channel_label == "Bez kontakta"


def test_a_link_alone_is_a_way_to_apply():
    preview = build_preview(PasteValues(text="Posao u Osijeku", url="https://fb.example/p/1"))
    assert preview.channel == "site" and preview.values.url == "https://fb.example/p/1"


def test_a_very_long_text_is_cut_and_flagged():
    preview = build_preview(PasteValues(text="riječ " * (MAX_CHARS // 4)))
    assert preview.shortened is True and len(preview.values.text) <= MAX_CHARS


def test_second_step_keeps_only_what_was_filled_in():
    values, overrides, errors = parse_second_step({
        "text": "Posao", "url": "", "title": "  Moj   naslov ", "employer": "", "city": "Đakovo",
        "pay": "8,5"})
    assert errors == {} and overrides == {"title": "Moj naslov", "city": "Đakovo",
                                          "pay_eur_hour": 8.5}
    assert values.title == "Moj naslov" and values.employer == ""
    assert parse_second_step({"text": "Posao"})[1] == {}


def test_second_step_checks_lengths_and_pay():
    _, _, errors = parse_second_step({"text": "x", "title": "n" * 301, "employer": "e" * 301,
                                      "city": "c" * 121, "pay": "abc"})
    assert set(errors) == {"title", "employer", "city", "pay"}


# ---------------------------------------------------------------- the pages
@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    return TestClient(app, follow_redirects=False)


def post(client, path, **data):
    return client.post(path, data=data, headers=ORIGIN)


def test_step_one_page(client):
    r = client.get("/dodaj")
    assert r.status_code == 200 and "<h1>Dodaj oglas</h1>" in r.text
    assert 'action="/dodaj/pregled"' in r.text and 'name="text"' in r.text
    assert "nikada sama ne otvara" in r.text  # the link is stored, never opened
    assert "Natrag na poslove" in r.text


def test_the_inbox_offers_the_form(client):
    assert 'href="/dodaj"' in client.get("/poslovi").text


def test_preview_shows_what_was_understood_and_saves_nothing(client, db):
    r = post(client, "/dodaj/pregled", text=FACEBOOK_AD, url="")
    assert r.status_code == 200
    for text in ("Što je aplikacija razumjela", 'value="Caffe Bar Luna d.o.o."', 'value="Osijek"',
                 'value="7,00"', "kafic@example.hr", "Prepoznati način javljanja: <strong>E-mail",
                 "Spremi oglas", "Promijeni tekst", 'action="/dodaj/spremi"'):
        assert text in r.text, text
    assert "Traži se konobar/ica za vikend" in r.text
    assert db.query(Job).count() == 0 and db.query(Source).count() == 0


def test_step_one_errors(client):
    r = post(client, "/dodaj/pregled", text="", url="javascript:alert(1)")
    assert r.status_code == 200 and "Oglas nije spremljen." in r.text
    assert "Zalijepite tekst oglasa." in r.text and "http:// ili https://" in r.text
    assert 'aria-invalid="true"' in r.text and 'value="javascript' not in r.text


def test_change_text_goes_back_with_the_text_kept(client):
    r = post(client, "/dodaj/uredi", text="Moj tekst", url="https://fb.example/p/1")
    assert r.status_code == 200 and "Moj tekst</textarea>" in r.text
    assert 'value="https://fb.example/p/1"' in r.text and 'action="/dodaj/pregled"' in r.text


def test_saving_creates_a_scored_ad_and_opens_it(client, db):
    db.add(Place(label="Dom", kind="home", lat=45.555, lon=18.6955,
                 max_minutes_by_mode={"bus": 45}, active=True))
    db.flush()
    r = post(client, "/dodaj/spremi", text=FACEBOOK_AD, url="https://fb.example/p/1",
             title="Konobar vikend", employer="Luna", city="Čepin", pay="9,5")
    job = db.query(Job).one()
    assert r.status_code == 303 and r.headers["location"] == f"/poslovi/{job.id}?ok=dodano"
    assert (job.title, job.employer, job.city, job.pay_eur_hour) == (
        "Konobar vikend", "Luna", "Čepin", 9.5)
    assert job.url == "https://fb.example/p/1" and job.source.key == "paste"
    assert job.score is not None and job.channel == "email" and job.status == "new"
    assert "091 234 5678" in job.description
    page = client.get(r.headers["location"]).text
    assert "Oglas je dodan." in page and "Konobar vikend" in page
    assert "Konobar vikend" in client.get("/poslovi").text


def test_an_ad_too_far_away_is_saved_but_says_why_it_is_set_aside(client, db):
    db.add(Place(label="Dom", kind="home", lat=45.555, lon=18.6955,
                 max_minutes_by_mode={"bus": 45}, active=True))
    db.flush()
    r = post(client, "/dodaj/spremi", text="Konobar u Zagrebu", url="", city="Zagreb")
    job = db.query(Job).one()
    assert (job.status, job.score) == ("ignored", 0)
    page = client.get(r.headers["location"]).text
    assert "Oglas je dodan." in page
    assert "Ne odgovara vašem profilu: predaleko od svih vaših mjesta." in page


def test_empty_fields_use_the_guess(client, db):
    post(client, "/dodaj/spremi", text=FACEBOOK_AD, url="", title="", employer="", city="", pay="")
    job = db.query(Job).one()
    assert job.title.startswith("Traži se konobar/ica") and job.employer == "Caffe Bar Luna d.o.o."
    assert job.city == "Osijek" and job.pay_eur_hour == 7.0 and job.url.startswith("paste:")


def test_an_ad_without_a_link_or_contact_has_no_way_to_apply(client, db):
    post(client, "/dodaj/spremi", text="Treba nam pomoć u skladištu u Osijeku.", url="")
    assert db.query(Job).one().channel == "none"


def test_pasting_the_same_ad_twice_gives_the_first_one(client, db):
    first = post(client, "/dodaj/spremi", text=FACEBOOK_AD, url="")
    second = post(client, "/dodaj/spremi", text=FACEBOOK_AD, url="")
    assert db.query(Job).count() == 1
    assert second.headers["location"] == f"{first.headers['location'].split('?')[0]}?ok=vec_postoji"
    assert "Taj oglas već postoji" in client.get(second.headers["location"]).text


def test_an_ad_that_is_a_copy_of_a_stored_one_opens_the_original(client, db):
    src = Source(key="sp", name="studentski-poslovi.hr")
    db.add(src)
    original = Job(source=src, external_id="x1", url="https://example.hr/1", employer="Luna d.o.o.",
                   title="Konobar vikend", city="Osijek", description=FACEBOOK_AD, score=60)
    db.add(original)
    db.flush()
    r = post(client, "/dodaj/spremi", text=FACEBOOK_AD + " Javite se!", url="",
             title="Konobar vikend", employer="Luna d.o.o.", city="Osijek")
    assert r.status_code == 303
    assert r.headers["location"] == f"/poslovi/{original.id}?ok=vec_postoji"
    db.expire_all()
    assert db.query(Job).filter(Job.duplicate_of == original.id).count() == 1


def test_a_bad_field_returns_to_the_review_with_the_typing_kept(client, db):
    r = post(client, "/dodaj/spremi", text=FACEBOOK_AD, url="", title="Moj naslov", employer="",
             city="", pay="puno")
    assert r.status_code == 200 and "Plaća: upišite broj" in r.text
    assert 'value="Moj naslov"' in r.text and 'value="puno"' in r.text
    assert 'aria-invalid="true"' in r.text and "Spremi oglas" in r.text
    assert db.query(Job).count() == 0


def test_the_review_without_text_starts_over(client, db):
    r = post(client, "/dodaj/spremi", text="", url="", title="x")
    assert r.status_code == 200 and "Zalijepite tekst oglasa." in r.text
    assert 'action="/dodaj/pregled"' in r.text and db.query(Job).count() == 0


def test_pasted_text_is_only_ever_text(client, db):
    evil = '<script>alert(1)</script> "><img src=x onerror=alert(2)> posao u Osijeku'
    html = post(client, "/dodaj/pregled", text=evil, url="").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    post(client, "/dodaj/spremi", text=evil, url="", title='"><b>x</b>')
    job = db.query(Job).one()
    page = client.get(f"/poslovi/{job.id}").text
    assert "<script>alert(1)" not in page and "<b>x</b>" not in page


def test_nothing_is_fetched_from_the_link_or_anywhere(client, monkeypatch):
    import smtplib
    import socket

    import httpx

    def boom(*a, **k):
        raise AssertionError("the app must not contact anybody here")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)
    monkeypatch.setattr(smtplib.SMTP, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    url = "https://www.facebook.com/groups/1/posts/2"
    assert post(client, "/dodaj/pregled", text=FACEBOOK_AD, url=url).status_code == 200
    assert post(client, "/dodaj/spremi", text=FACEBOOK_AD, url=url).status_code == 303


def test_posts_must_come_from_this_app(client, db):
    for path in ("/dodaj/pregled", "/dodaj/uredi", "/dodaj/spremi"):
        assert client.post(path, data={"text": FACEBOOK_AD}).status_code == 403
        assert client.post(path, data={"text": FACEBOOK_AD},
                           headers={"origin": "http://evil.example"}).status_code == 403
    assert db.query(Job).count() == 0
