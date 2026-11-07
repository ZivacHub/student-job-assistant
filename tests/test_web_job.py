"""Step 4.3 (page): the page of one ad, its actions and the employer box."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.main import create_app
from app.models import Company, Contact, Event, Job, Source
from app.models.base import utcnow

ORIGIN = {"origin": "http://testserver"}


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


def add(db, src, n=1, contacts=(), **kw):
    values = dict(source=src, external_id=f"e{n}", url=f"https://example.hr/oglas/{n}",
                  title=f"Posao {n}", status="seen", channel="email", score=50,
                  first_seen_at=utcnow() - timedelta(minutes=n))
    values.update(kw)
    job = Job(**values)
    for kind, value, from_ad in contacts:
        job.contacts.append(Contact(kind=kind, value=value, from_ad=from_ad))
    db.add(job)
    db.flush()
    return job


def events(db, job):
    return [e.kind for e in db.query(Event).filter(Event.job_id == job.id).order_by(Event.id)]


def action(client, job, name, **extra):
    return client.post(f"/poslovi/{job.id}/radnja", data={"akcija": name, **extra}, headers=ORIGIN)


# ---------------------------------------------------------------- the page
def test_page_shows_what_there_is_to_know_about_the_ad(client, db, src):
    job = add(db, src, title="Konobar/ica vikendom", employer="Caffe bar Tvrđa d.o.o.",
              city="Osijek", category="ugostiteljstvo", pay_eur_hour=7.5, score=83, channel="email",
              description="Prvi red opisa.\nDrugi red opisa.", address="Trg 1",
              score_reasons=[
                  {"k": "category", "pts": 25, "text": "x",
                   "data": {"category": "ugostiteljstvo", "wanted": True}},
                  {"k": "travel", "pts": 20, "text": "x",
                   "data": {"minutes": 12, "mode": "car", "place": "Dom"}}],
              contacts=[("email", "posao@primjer.hr", True)])
    r = client.get(f"/poslovi/{job.id}")
    assert r.status_code == 200
    html = r.text
    assert '<html lang="hr">' in html
    assert '<h1 class="jobhead__title">Konobar/ica vikendom</h1>' in html
    for text in ("Caffe bar Tvrđa d.o.o. · Osijek", "7,50 €/sat",
                 "<strong>12 min</strong> autom od: Dom", "Osijek, Trg 1", "ugostiteljstvo",
                 "studentski-poslovi.hr", "Kako se javiti",
                 "posao@primjer.hr", "Prvi red opisa.\nDrugi red opisa.", "Zašto ova ocjena",
                 "Vrsta posla koju tražite: ugostiteljstvo", "12 min autom od: Dom",
                 "Otvori izvorni oglas", "Natrag na poslove", ">83</p>"):
        assert text in html, text
    assert 'href="https://example.hr/oglas/1"' in html and 'rel="noopener noreferrer"' in html
    assert "NOVI OGLAS: Konobar/ica vikendom" in html  # the text to copy
    assert "Slanje e-pošte poslodavcu još nije uključeno" in html


def test_opening_the_page_marks_a_new_ad_as_seen_without_a_log_entry(client, db, src):
    job = add(db, src, status="new")
    client.get(f"/poslovi/{job.id}")
    db.expire_all()
    assert db.get(Job, job.id).status == "seen" and events(db, job) == []


def test_opening_the_page_does_not_touch_other_states(client, db, src):
    job = add(db, src, status="ignored", score=70)
    client.get(f"/poslovi/{job.id}")
    db.expire_all()
    assert db.get(Job, job.id).status == "ignored"


@pytest.mark.parametrize("raw", ["999999", "abc", "-1", "0x10", "1.5", "%C2%B2", "99999999999999",
                                 "1%20OR%201=1"])
def test_unknown_or_odd_ids_give_a_normal_not_found_page(client, raw):
    r = client.get(f"/poslovi/{raw}")
    assert r.status_code == 404
    assert "Oglas nije pronađen" in r.text and 'href="/poslovi"' in r.text


def test_actions_on_a_missing_ad_are_not_found(client):
    assert client.post("/poslovi/999/radnja", data={"akcija": "zanemari"}, headers=ORIGIN
                       ).status_code == 404
    assert client.post("/poslovi/999/poslodavac", data={}, headers=ORIGIN).status_code == 404


def test_set_aside_ad_explains_why(client, db, src):
    job = add(db, src, status="ignored", score=0, score_reasons=[
        {"k": "travel", "pts": 0, "text": "Too far", "data": {}}])
    html = client.get(f"/poslovi/{job.id}").text
    assert "Ne odgovara vašem profilu: predaleko od svih vaših mjesta." in html
    assert "Vrati među otvorene" in html


def test_ad_without_description_or_contacts_still_works(client, db, src):
    job = add(db, src, channel="none", description=None, employer=None, url="", score=None)
    html = client.get(f"/poslovi/{job.id}").text
    assert "Oglas nema opis." in html and "U oglasu nije naveden način prijave." in html
    assert "Oglas ne navodi poslodavca" in html and "Otvori izvorni oglas" not in html


# ---------------------------------------------------------------- untrusted text
def test_everything_from_the_ad_is_escaped(client, db, src):
    job = add(db, src, title='<script>alert("t")</script>',
              employer='"><img src=x onerror=alert(1)>', city="<b>Osijek</b>",
              description="<iframe src=//evil></iframe>\n<b>podebljano</b>",
              address="<u>ulica</u>", contacts=[("email", "a<b>@x.hr", True)])
    html = client.get(f"/poslovi/{job.id}").text
    for raw in ('<script>alert("t")', "<img src=x", "<iframe", "<b>podebljano", "<u>ulica",
                "a<b>@x.hr"):
        assert raw not in html, raw
    assert "&lt;iframe src=//evil&gt;&lt;/iframe&gt;" in html
    assert 'src="/static/job.js"' in html
    tags = re.findall(r"<script[^>]*>", html)
    assert tags and all('src="/static/' in t for t in tags)  # no inline script


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "data:text/html,hi", "vbscript:x", "/x", ""])
def test_the_ads_own_address_is_a_button_only_when_it_is_http(client, db, src, url):
    job = add(db, src, url=url)
    html = client.get(f"/poslovi/{job.id}").text
    assert "Otvori izvorni oglas" not in html
    for scheme in ("javascript", "data", "vbscript"):
        assert f'href="{scheme}' not in html


def test_a_quote_in_the_address_cannot_break_out_of_the_attribute(client, db, src):
    job = add(db, src, url='https://example.hr/x" onmouseover="alert(1)')
    html = client.get(f"/poslovi/{job.id}").text
    assert 'onmouseover="alert(1)"' not in html and "&#34; onmouseover=&#34;" in html


def test_links_found_in_the_ad_are_only_clickable_when_http(client, db, src):
    job = add(db, src, contacts=[("link", "https://prijava.example.hr/forma", True),
                                 ("link", "javascript:alert(1)", True)])
    html = client.get(f"/poslovi/{job.id}").text
    link = 'href="https://prijava.example.hr/forma" target="_blank" rel="noopener noreferrer"'
    assert link in html
    assert 'href="javascript' not in html and "javascript:alert(1)" in html  # shown, not a link


def test_phone_numbers_become_call_links_only_when_they_are_plain_digits(client, db, src):
    job = add(db, src, channel="phone", contacts=[("phone", "+38531123456", True),
                                                  ("phone", "javascript:alert(1)", True)])
    html = client.get(f"/poslovi/{job.id}").text
    assert '<strong>031 123 456</strong>' in html and 'href="tel:+38531123456"' in html
    assert 'href="tel:javascript' not in html and 'href="javascript' not in html


def test_a_typed_in_number_is_marked_as_such(client, db, src):
    job = add(db, src, channel="phone", contacts=[("phone", "+38531123456", False)])
    assert "(upisali ste ručno)" in client.get(f"/poslovi/{job.id}").text


# ---------------------------------------------------------------- buttons
def test_buttons_follow_the_state(client, db, src):
    job = add(db, src, status="seen")
    html = client.get(f"/poslovi/{job.id}").text
    for text in ("Prijavio/la sam se", "Odgodi: za 3 sata", "Odgodi: sutra u 8:00", ">Zanemari<"):
        assert text in html, text
    assert "Vrati među otvorene" not in html
    assert "Nikome se ništa ne šalje." in html
    job.status = "ignored"
    db.flush()
    html = client.get(f"/poslovi/{job.id}").text
    assert "Vrati među otvorene" in html
    assert "Prijavio/la sam se" not in html and ">Zanemari<" not in html and "Odgodi:" not in html


# ---------------------------------------------------------------- actions
@pytest.mark.parametrize("name, status, kind", [
    ("zanemari", "ignored", "job_ignored"),
    ("prijavljeno", "applied", "job_applied"),
    ("odgodi", "snoozed", "job_snoozed"),
])
def test_each_action_changes_the_state_and_logs_an_event(client, db, src, name, status, kind):
    job = add(db, src, status="seen")
    r = action(client, job, name)
    assert r.status_code == 303 and r.headers["location"] == f"/poslovi/{job.id}?ok={name}"
    db.expire_all()
    assert db.get(Job, job.id).status == status
    assert events(db, job) == [kind]


def test_bring_back_after_ignoring(client, db, src):
    job = add(db, src, status="seen")
    action(client, job, "zanemari")
    r = action(client, job, "vrati")
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Job, job.id).status == "seen"
    assert events(db, job) == ["job_ignored", "job_reopened"]


def test_the_message_after_an_action(client, db, src):
    job = add(db, src, status="seen")
    action(client, job, "zanemari")
    html = client.get(f"/poslovi/{job.id}?ok=zanemari").text
    assert 'role="status">Oglas je zanemaren.' in html
    assert 'alert--info' not in html  # the state note would only repeat the message
    other = client.get(f"/poslovi/{job.id}?ok=dodano").text  # a message that does not repeat it
    assert 'alert--info' in other
    assert 'alert--info' in client.get(f"/poslovi/{job.id}").text  # without a message it shows
    assert "role=\"status\">" not in client.get(f"/poslovi/{job.id}?ok=nonsense").text.split(
        "jobhead__badges")[1].split("detail")[0]  # an unknown message key shows nothing


def test_snooze_hides_the_ad_from_the_inbox_until_it_is_due(client, db, src):
    job = add(db, src, title="Odgodi me", status="seen")
    action(client, job, "odgodi", trajanje="sutra")
    page = client.get(f"/poslovi/{job.id}").text
    assert "Odgođeno do " in page and "u 08:00." in page
    assert "Odgodi me" not in client.get("/poslovi").text
    assert "Odgodi me" in client.get("/poslovi?prikaz=sve").text
    # time passes: the ad is due
    db.expire_all()
    db.get(Job, job.id).snoozed_until = utcnow() - timedelta(minutes=1)
    db.flush()
    assert "Odgodi me" in client.get("/poslovi").text
    db.expire_all()
    assert db.get(Job, job.id).status == "seen" and db.get(Job, job.id).snoozed_until is None
    assert events(db, job) == ["job_snoozed", "job_unsnoozed"]


def test_a_nonsense_action_shows_a_message_and_changes_nothing(client, db, src):
    job = add(db, src, status="seen")
    bad = ({"akcija": "obrisi"}, {"akcija": ""}, {}, {"akcija": "odgodi", "trajanje": "zauvijek"})
    for data in bad:
        r = client.post(f"/poslovi/{job.id}/radnja", data=data, headers=ORIGIN)
        assert r.status_code == 200 and 'role="alert"' in r.text
    db.expire_all()
    assert db.get(Job, job.id).status == "seen" and events(db, job) == []


def test_an_action_that_does_not_fit_is_refused(client, db, src):
    job = add(db, src, status="applied")
    r = action(client, job, "zanemari")
    assert r.status_code == 200 and "Ta radnja nije moguća" in r.text
    db.expire_all()
    assert db.get(Job, job.id).status == "applied"


def test_posts_need_to_come_from_this_app(client, db, src):
    job = add(db, src, status="seen")
    r = client.post(f"/poslovi/{job.id}/radnja", data={"akcija": "zanemari"})
    assert r.status_code == 403
    r = client.post(f"/poslovi/{job.id}/radnja", data={"akcija": "zanemari"},
                    headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post(f"/poslovi/{job.id}/poslodavac", data={"phone": "031 123 456"})
    assert r.status_code == 403
    db.expire_all()
    assert db.get(Job, job.id).status == "seen"
    assert db.query(Company).count() == 0


def test_actions_never_contact_anybody(client, db, src, monkeypatch):
    import smtplib
    import socket

    import httpx

    def boom(*a, **k):
        raise AssertionError("the app must not contact anybody here")

    # no outside web request, no mail connection, no name lookup (the test client itself talks
    # to the app in memory and uses none of these)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)
    monkeypatch.setattr(smtplib.SMTP, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    job = add(db, src, employer="Neka tvrtka d.o.o.",
              contacts=[("email", "posao@primjer.hr", True)])
    assert client.get(f"/poslovi/{job.id}").status_code == 200
    for name in ("odgodi", "zanemari", "vrati", "prijavljeno"):
        assert action(client, job, name).status_code == 303
    r = client.post(f"/poslovi/{job.id}/poslodavac", data={"phone": "031 123 456"}, headers=ORIGIN)
    assert r.status_code == 303


def test_the_inbox_title_opens_this_page(client, db, src):
    job = add(db, src, title="Idemo")
    assert f'<a href="/poslovi/{job.id}">Idemo</a>' in client.get("/poslovi").text


# ---------------------------------------------------------------- the employer box
def test_the_box_has_the_companywall_button_and_says_nothing_is_fetched(client, db, src):
    job = add(db, src, employer="Momentum OS d.o.o.")
    html = client.get(f"/poslovi/{job.id}").text
    assert "Podaci o poslodavcu" in html and "Otvori na CompanyWall" in html
    assert 'href="https://www.google.com/search?q=site%3Acompanywall.hr' in html
    assert "Momentum+OS" in html and 'target="_blank" rel="noopener noreferrer"' in html
    assert "aplikacija sama ništa ne dohvaća s CompanyWalla" in html


def test_saving_the_employer_phone_and_director(client, db, src):
    job = add(db, src, employer="Momentum OS d.o.o.", channel="site", url="https://example.hr/m")
    r = client.post(f"/poslovi/{job.id}/poslodavac", headers=ORIGIN, data={
        "phone": "031 123 456", "director_name": "  Ana   Horvat ", "director_role": "direktorica",
        "note": "zovite ujutro"})
    assert r.status_code == 303 and r.headers["location"] == f"/poslovi/{job.id}?ok=poslodavac"
    company = db.query(Company).one()
    assert (company.phone, company.director_name, company.director_role, company.note) == (
        "+38531123456", "Ana Horvat", "direktorica", "zovite ujutro")
    db.expire_all()
    saved = db.get(Job, job.id)
    assert saved.channel == "phone"  # an ad with no way to apply is now a phone job
    contacts = [(c.kind, c.value, c.from_ad) for c in saved.contacts]
    assert contacts == [("phone", "+38531123456", False)]
    page = client.get(f"/poslovi/{job.id}?ok=poslodavac").text
    assert "Podaci o poslodavcu su spremljeni." in page
    assert 'value="031 123 456"' not in page and 'value="+38531123456"' in page
    assert 'value="Ana Horvat"' in page and "zovite ujutro</textarea>" in page
    assert "(upisali ste ručno)" in page and "<strong>031 123 456</strong>" in page
    assert "Dobar dan, mogu li govoriti s Ana Horvat?" in page  # the phone script uses the name


def test_the_log_records_that_the_box_was_saved_but_not_the_personal_data(client, db, src):
    job = add(db, src, employer="Momentum OS d.o.o.")
    client.post(f"/poslovi/{job.id}/poslodavac", headers=ORIGIN,
                data={"phone": "031 123 456", "director_name": "Ana Horvat"})
    [event] = db.query(Event).filter(Event.job_id == job.id).all()
    assert event.kind == "company_info_saved"
    assert event.data == {"employer": "Momentum OS d.o.o.", "fields": ["phone", "director_name"]}
    assert "031" not in str(event.data) and "Ana" not in str(event.data)


def test_a_bad_phone_number_is_explained_and_nothing_is_saved(client, db, src):
    job = add(db, src, employer="Momentum OS d.o.o.")
    r = client.post(f"/poslovi/{job.id}/poslodavac", headers=ORIGIN,
                    data={"phone": "12ab", "director_name": "Ana Horvat", "note": "bilješka"})
    assert r.status_code == 200
    assert "Podaci nisu spremljeni." in r.text and "Taj broj telefona nije valjan." in r.text
    assert 'aria-invalid="true"' in r.text and 'value="12ab"' in r.text
    assert 'value="Ana Horvat"' in r.text  # what was typed is kept
    assert db.query(Company).count() == 0 and events(db, job) == []


def test_an_empty_field_clears_what_was_saved(client, db, src):
    job = add(db, src, employer="Momentum OS d.o.o.")
    client.post(f"/poslovi/{job.id}/poslodavac", headers=ORIGIN,
                data={"phone": "031 123 456", "director_name": "Ana Horvat"})
    client.post(f"/poslovi/{job.id}/poslodavac", headers=ORIGIN,
                data={"phone": "", "director_name": "", "director_role": "", "note": ""})
    company = db.query(Company).one()
    assert (company.phone, company.director_name, company.director_role, company.note) == (
        None, None, None, None)


def test_the_same_employer_written_differently_shares_one_box(client, db, src):
    first = add(db, src, 1, employer="MOMENTUM OS d.o.o.")
    second = add(db, src, 2, employer="Momentum OS doo")
    client.post(f"/poslovi/{first.id}/poslodavac", headers=ORIGIN, data={"phone": "031 123 456"})
    assert 'value="+38531123456"' in client.get(f"/poslovi/{second.id}").text
    assert db.query(Company).count() == 1


def test_text_typed_into_the_box_is_escaped_when_shown_again(client, db, src):
    job = add(db, src, employer="Momentum OS d.o.o.")
    client.post(f"/poslovi/{job.id}/poslodavac", headers=ORIGIN, data={
        "director_name": '"><script>alert(1)</script>',
        "note": "</textarea><script>alert(2)</script>"})
    html = client.get(f"/poslovi/{job.id}").text
    assert "<script>alert" not in html and "</textarea><script>" not in html


def test_an_ad_without_an_employer_cannot_get_employer_info(client, db, src):
    job = add(db, src, employer=None)
    r = client.post(f"/poslovi/{job.id}/poslodavac", headers=ORIGIN, data={"phone": "031 123 456"})
    assert r.status_code == 200 and "nema naziv poslodavca" in r.text
    assert db.query(Company).count() == 0


# ---------------------------------------------------------------- copy button
def test_copy_text_is_on_the_page_and_the_button_starts_hidden(client, db, src):
    job = add(db, src, title="Za kopiranje", description="Opis za kopiranje",
              contacts=[("email", "posao@primjer.hr", True)])
    html = client.get(f"/poslovi/{job.id}").text
    area = re.search(r'<textarea id="alert-text"[^>]*readonly[^>]*>(.*?)</textarea>', html, re.S)
    assert area and "Za kopiranje" in area.group(1) and "Opis za kopiranje" in area.group(1)
    assert f"http://testserver/poslovi/{job.id}" in area.group(1)
    button = re.search(r'<button[^>]*data-copy-from="alert-text"[^>]*>', html).group(0)
    assert " hidden" in button and 'type="button"' in button
    assert 'role="status" data-copy-status="alert-text"' in html


def test_the_copy_script_is_served(client):
    r = client.get("/static/job.js")
    assert r.status_code == 200 and "clipboard" in r.text and "DOMContentLoaded" in r.text


# ------------ phone-only and link-only ads (plan 5.5)
def test_a_phone_ad_has_a_one_tap_call_button_that_marks_it_and_logs_it(client, db, src):
    job = add(db, src, channel="phone", contacts=[("phone", "+38531123456", True)])
    html = client.get(f"/poslovi/{job.id}").text
    assert '<button class="btn btn--primary" type="submit">Nazvao/la sam</button>' in html
    assert 'value="nazvano"' in html
    r = client.post(f"/poslovi/{job.id}/radnja", data={"akcija": "nazvano"}, headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == f"/poslovi/{job.id}?ok=nazvano"
    assert events(db, job)[-1] == "job_called" and job.status == "applied"
    page = client.get(r.headers["location"]).text
    assert "Označeno je da ste nazvali poslodavca." in page


def test_a_link_ad_says_the_application_was_made_on_the_site(client, db, src):
    job = add(db, src, channel="site")
    html = client.get(f"/poslovi/{job.id}").text
    assert "Prijavio/la sam se na stranici" in html and "Nazvao/la sam" not in html


def test_an_email_ad_keeps_the_plain_applied_button_and_has_no_call_button(client, db, src):
    job = add(db, src, channel="email", contacts=[("email", "posao@primjer.hr", True)])
    html = client.get(f"/poslovi/{job.id}").text
    assert ">Prijavio/la sam se</button>" in html and "Nazvao/la sam" not in html
    r = client.post(f"/poslovi/{job.id}/radnja", data={"akcija": "nazvano"}, headers=ORIGIN)
    assert r.status_code == 200 and "nije moguća" in r.text  # refused: not a phone ad
