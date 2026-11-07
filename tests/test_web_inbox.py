"""Step 4.1 (page): the Poslovi screen (Inbox) as the browser gets it."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.main import create_app
from app.models import Job, Place, Source
from app.models.base import utcnow


@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def src(db):
    s = Source(key="t", name="studentski-poslovi.hr")
    db.add(s)
    db.flush()
    return s


def add(db, src, n, **kw):
    values = dict(source=src, external_id=f"e{n}", url=f"https://example.hr/{n}",
                  title=f"Posao {n}", status="seen", channel="email", score=50,
                  first_seen_at=utcnow() - timedelta(minutes=n))
    values.update(kw)
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def add_home(db):
    db.add(Place(label="Dom", kind="home", lat=45.55, lon=18.69, max_minutes_by_mode={"car": 30}))
    db.flush()


def order(html, *titles):
    positions = [html.index(t) for t in titles]
    found = dict(zip(titles, positions, strict=True))
    assert positions == sorted(positions), f"wrong order: {found}"


# ---------------------------------------------------------------- the page itself
def test_home_is_the_inbox(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/poslovi"


def test_empty_inbox_says_what_is_going_on(client):
    r = client.get("/poslovi")
    assert r.status_code == 200
    assert '<html lang="hr">' in r.text and "<h1>Poslovi</h1>" in r.text
    assert "Još nema spremljenih oglasa." in r.text
    assert "0 oglasa na koje se još možete javiti" in r.text


def test_navigation_has_both_screens_and_marks_the_current_one(client):
    html = client.get("/poslovi").text
    assert '<a href="/poslovi" aria-current="page">Poslovi</a>' in html
    assert '<a href="/mjesta" >Mjesta i profil</a>' in html
    assert '<a href="/mjesta" aria-current="page">' in client.get("/mjesta").text


def test_a_row_shows_score_title_employer_town_travel_pay_channel_and_age(client, db, src):
    add_home(db)
    job = add(db, src, 7, title="Konobar/ica vikendom", employer="Primjer d.o.o.", city="Osijek",
        score=83, pay_eur_hour=7.5, channel="phone", status="new",
        score_reasons=[{"k": "travel", "pts": 20, "text": "x",
                        "data": {"minutes": 12, "mode": "car", "place": "Dom"}}])
    html = client.get("/poslovi").text
    for text in ("Konobar/ica vikendom", "Primjer d.o.o. · Osijek",
                 "<strong>12 min</strong> autom od: Dom", "7,50 €/sat", "Telefon", "Novo",
                 "prije 7 min", "studentski-poslovi.hr", ">83</p>",
                 "1 oglas na koje se još možete javiti"):
        assert text in html, text
    assert 'class="job job--high"' in html
    assert f'<a href="/poslovi/{job.id}">Konobar/ica vikendom</a>' in html  # opens the job page
    assert "example.hr" not in html  # the ad's own address is on the job page, not in the list


def test_best_score_comes_first_in_the_page(client, db, src):
    add(db, src, 1, title="Srednji posao", score=55)
    add(db, src, 2, title="Najbolji posao", score=92)
    add(db, src, 3, title="Slabi posao", score=12)
    order(client.get("/poslovi").text, "Najbolji posao", "Srednji posao", "Slabi posao")


def test_missing_pay_and_unchecked_travel_are_said_plainly(client, db, src):
    add(db, src, 1, score_reasons=[
        {"k": "travel", "pts": 12, "text": "x", "data": {"unknown": True}}])
    html = client.get("/poslovi").text
    assert "Plaća nije navedena" in html and "Put nije izračunat" in html


def test_notice_to_add_places_only_while_there_are_none(client, db, src):
    add(db, src, 1)
    assert "niste unijeli nijedno svoje mjesto" in client.get("/poslovi").text
    add_home(db)
    assert "niste unijeli nijedno svoje mjesto" not in client.get("/poslovi").text


# ---------------------------------------------------------------- text from ads is never trusted
EVIL = '<script>alert("x")</script>'


def test_ad_text_is_escaped_everywhere(client, db, src):
    add(db, src, 1, title=EVIL, employer='"><img src=x onerror=alert(1)>', city="<b>Osijek</b>",
        pay_text="<i>mnogo</i>")
    html = client.get("/poslovi").text
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "<b>Osijek</b>" not in html and "<i>mnogo</i>" not in html
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in html
    assert "&lt;i&gt;mnogo&lt;/i&gt;" in html


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "data:text/html,hi", "vbscript:x", "/relative", ""])
def test_a_link_that_is_not_http_is_not_made_clickable(client, db, src, url):
    add(db, src, 1, title="Opasan link", url=url)
    html = client.get("/poslovi").text
    assert "Opasan link" in html
    for scheme in ("javascript", "data", "vbscript"):
        assert f'href="{scheme}' not in html
    assert 'rel="noopener noreferrer"' not in html


def test_the_list_never_carries_an_address_taken_from_an_ad(client, db, src):
    add(db, src, 1, title="Link", url='https://example.hr/x" onmouseover="alert(1)')
    html = client.get("/poslovi").text
    assert "onmouseover" not in html and "example.hr" not in html


# ---------------------------------------------------------------- filters
def test_view_filter_open_new_and_all(client, db, src):
    add(db, src, 1, title="Nov oglas", status="new")
    add(db, src, 2, title="Vidjen oglas", status="seen")
    add(db, src, 3, title="Zanemaren oglas", status="ignored", score=60)
    add(db, src, 4, title="Prijavljen oglas", status="applied")
    default = client.get("/poslovi").text
    assert "Nov oglas" in default and "Vidjen oglas" in default
    assert "Zanemaren oglas" not in default and "Prijavljen oglas" not in default
    only_new = client.get("/poslovi?prikaz=nove").text
    assert "Nov oglas" in only_new and "Vidjen oglas" not in only_new
    everything = client.get("/poslovi?prikaz=sve").text
    for t in ("Nov oglas", "Vidjen oglas", "Zanemaren oglas", "Prijavljen oglas"):
        assert t in everything
    assert "Zanemareno" in everything and "Prijavljeno" in everything


def test_channel_and_score_filters(client, db, src):
    add(db, src, 1, title="Mail visoko", channel="email", score=85)
    add(db, src, 2, title="Mail nisko", channel="email", score=20)
    add(db, src, 3, title="Telefon visoko", channel="phone", score=90)
    by_channel = client.get("/poslovi?kanal=phone").text
    assert "Telefon visoko" in by_channel and "Mail visoko" not in by_channel
    by_score = client.get("/poslovi?ocjena=60").text
    assert "Mail visoko" in by_score and "Telefon visoko" in by_score
    assert "Mail nisko" not in by_score
    both = client.get("/poslovi?kanal=email&ocjena=60").text
    assert "Mail visoko" in both and "Telefon visoko" not in both and "Mail nisko" not in both


def test_the_form_shows_the_chosen_filters_and_counts(client, db, src):
    add(db, src, 1, status="new")
    add(db, src, 2, status="seen")
    add(db, src, 3, status="ignored", score=40)
    html = client.get("/poslovi?prikaz=sve&kanal=email&ocjena=60").text
    assert '<option value="sve" selected>Svi oglasi (3)</option>' in html
    assert '<option value="nove" >Samo nove (1)</option>' in html
    assert '<option value="otvoreni" >Otvoreni (2)</option>' in html
    assert '<option value="email" selected>E-mail</option>' in html
    assert '<option value="60" selected>60 i više</option>' in html
    assert 'href="/poslovi">Poništi</a>' in html
    assert "Poništi" not in client.get("/poslovi").text


def test_an_odd_score_from_the_address_still_shows_as_chosen(client):
    html = client.get("/poslovi?ocjena=73").text
    assert '<option value="73" selected>73 i više</option>' in html


@pytest.mark.parametrize("query", [
    "?prikaz=<script>", "?kanal=%00", "?ocjena=abc", "?ocjena=-5", "?stranica=999999999999",
    "?ocjena=%C2%B2&stranica=%D9%A3",
])
def test_broken_addresses_still_give_a_normal_page(client, db, src, query):
    add(db, src, 1)
    r = client.get("/poslovi" + query)
    assert r.status_code == 200 and "Posao 1" in r.text


def test_a_repeated_parameter_still_gives_a_normal_page(client):
    assert client.get("/poslovi?prikaz=sve&prikaz=nove&ocjena=1&ocjena=2").status_code == 200


def test_set_aside_ads_show_only_in_all_and_say_why(client, db, src):
    add(db, src, 1, title="Dobar posao", score=70)
    add(db, src, 2, title="Predaleko posao", status="ignored", score=0,
        score_reasons=[{"k": "travel", "pts": 0, "text": "Too far", "data": {}}])
    default = client.get("/poslovi").text
    assert "Predaleko posao" not in default
    assert "Skriveno: 1 oglas" in default and 'href="/poslovi?prikaz=sve"' in default
    everything = client.get("/poslovi?prikaz=sve").text
    assert "Predaleko posao" in everything
    assert "Ne odgovara: predaleko od svih vaših mjesta." in everything
    assert "Skriveno:" not in everything
    assert 'class="job job--off"' in everything


def test_empty_result_offers_the_way_out(client, db, src):
    add(db, src, 1, title="Samo ovaj", status="ignored", score=50)
    html = client.get("/poslovi").text
    assert "Nijedan oglas ne odgovara odabranim filterima." in html
    assert "Skriveno je 1 oglas" in html
    assert 'href="/poslovi?prikaz=sve">Prikaži sve oglase</a>' in html
    assert "Još nema spremljenih oglasa." not in html


def test_duplicates_do_not_appear_or_count(client, db, src):
    first = add(db, src, 1, title="Izvorni oglas")
    add(db, src, 2, title="Kopija oglasa", duplicate_of=first.id)
    html = client.get("/poslovi?prikaz=sve").text
    assert "Izvorni oglas" in html and "Kopija oglasa" not in html
    assert "1 oglas ukupno" in html


def test_pagination_links_keep_the_filters(client, db, src, monkeypatch):
    monkeypatch.setattr("app.core.inbox.PAGE_SIZE", 2)
    for i in range(5):
        add(db, src, i, title=f"Strana{i}", channel="email", score=90 - i)
    add(db, src, 9, title="Telefonski", channel="phone", score=99)
    page1 = client.get("/poslovi?kanal=email").text
    assert "Strana0" in page1 and "Strana2" not in page1 and "Telefonski" not in page1
    assert "Stranica 1 od 3" in page1 and "Prethodna" not in page1
    assert 'href="/poslovi?kanal=email&amp;stranica=2">Sljedeća</a>' in page1
    page3 = client.get("/poslovi?kanal=email&stranica=3").text
    assert "Strana4" in page3 and "Sljedeća" not in page3
    assert 'href="/poslovi?kanal=email&amp;stranica=2">Prethodna</a>' in page3
    assert "Stranica 3 od 3" in client.get("/poslovi?kanal=email&stranica=50").text


def test_no_paging_for_a_short_list(client, db, src):
    add(db, src, 1)
    assert "Stranica " not in client.get("/poslovi").text


# ---------------------------------------------------------------- reading only
def test_the_page_never_changes_anything(client, db, src):
    add(db, src, 1, status="new")
    client.get("/poslovi?prikaz=sve")
    db.expire_all()
    assert db.query(Job).one().status == "new"


def test_the_page_carries_the_security_headers(client):
    r = client.get("/poslovi")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    # no inline script (the policy would block it): every script tag loads a file of this app
    tags = re.findall(r"<script[^>]*>", r.text)
    assert tags and all('src="/static/' in tag for tag in tags)
