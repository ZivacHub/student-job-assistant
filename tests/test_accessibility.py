"""Step 4.6: accessibility details of the pages that a plain page test can see.

The rest (focus rings, contrast, tap-target size, the Tab-key walk) is measured in a real browser
by scripts/ui_check_a11y.js. Here: names, labels and hints that screen-reader users depend on.
"""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.main import create_app
from app.models import Contact, Job, Source
from app.models.base import utcnow

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    return TestClient(app, follow_redirects=False)


def add_job(db, src, phone="031 123 456"):
    job = Job(source=src, external_id="e1", url="https://example.hr/oglas/1", title="Konobar/ica",
              employer="Caffe Bar Luna d.o.o.", status="seen", channel="phone", score=60,
              first_seen_at=utcnow() - timedelta(minutes=1))
    job.contacts.append(Contact(kind="phone", value=phone, from_ad=True))
    db.add(job)
    db.flush()
    return job


# ---------------------------------------------------------------- a page with a problem gets focus
@pytest.mark.parametrize("path", ["/poslovi", "/izvori", "/dodaj", "/dnevnik", "/mjesta"])
def test_every_page_loads_the_script_that_focuses_the_problems(client, path):
    html = client.get(path).text
    assert '<script src="/static/forms.js" defer></script>' in html


def test_the_focus_script_is_served_and_looks_for_the_marked_message(client):
    r = client.get("/static/forms.js")
    assert r.status_code == 200
    assert "[data-error-summary]" in r.text and ".focus()" in r.text


# ---------------------------------------------------------------- Dodaj oglas
def test_the_paste_box_is_required_and_tied_to_its_hint(client):
    html = client.get("/dodaj").text
    box = re.search(r"<textarea[^>]*>", html).group(0)
    assert 'aria-required="true"' in box and "autofocus" in box
    assert 'aria-describedby="f-text-hint"' in box and 'id="f-text-hint"' in html
    assert 'aria-describedby="f-url-hint"' in html and 'id="f-url-hint"' in html


def test_a_refused_paste_form_puts_the_problems_first_not_the_box(client):
    page = client.post("/dodaj/pregled", data={"text": "", "url": "javascript:x"}, headers=ORIGIN)
    html = page.text
    assert "data-error-summary" in html
    box = re.search(r"<textarea[^>]*>", html).group(0)
    assert "autofocus" not in box  # the message takes the focus, not the empty box
    assert 'aria-invalid="true"' in box
    assert 'aria-describedby="f-text-err f-text-hint"' in box
    assert 'aria-describedby="f-url-err f-url-hint"' in html


# ---------------------------------------------------------------- Izvori: buttons say which source
def test_repeated_controls_on_izvori_name_their_source(client, db):
    db.add_all([Source(key="a", name="Prvi izvor", interval_minutes=10, enabled=True),
                Source(key="b", name="Drugi izvor", interval_minutes=10, enabled=True)])
    db.flush()
    html = client.get("/izvori").text
    for name in ("Prvi izvor", "Drugi izvor"):
        hidden = f'<span class="visually-hidden">: {name}</span>'
        for visible in ("Provjeri sada", "Spremi", "Uključeno", "Provjeravaj svakih"):
            assert f"{visible}{hidden}" in html, (visible, name)


# ---------------------------------------------------------------- Dnevnik: the link names the ad
def test_each_log_link_names_its_ad(client, db):
    from app.models import Event
    src = Source(key="t", name="studentski-poslovi.hr")
    db.add(src)
    db.flush()
    first = add_job(db, src)
    first.title = "Konobar/ica"
    second = Job(source=src, external_id="e2", url="https://example.hr/oglas/2", title="Skladištar",
                 status="seen", channel="phone", first_seen_at=utcnow())
    db.add(second)
    db.flush()
    for job in (first, second):
        db.add(Event(job_id=job.id, kind="job_ignored", data={}))
    db.flush()
    html = client.get("/dnevnik").text
    assert 'Otvori oglas<span class="visually-hidden"> Konobar/ica</span>' in html
    assert 'Otvori oglas<span class="visually-hidden"> Skladištar</span>' in html


# ---------------------------------------------------------------- job page
def test_the_call_link_says_which_number_it_dials(client, db):
    src = Source(key="t", name="studentski-poslovi.hr")
    db.add(src)
    db.flush()
    job = add_job(db, src, phone="+38531123456")
    html = client.get(f"/poslovi/{job.id}").text
    link = r'<a href="tel:[^"]+">Nazovi<span class="visually-hidden"> 031 123 456</span></a>'
    assert re.search(link, html)


def test_a_refused_employer_form_gets_focus_and_the_phone_is_tied_to_message_and_hint(client, db):
    src = Source(key="t", name="studentski-poslovi.hr")
    db.add(src)
    db.flush()
    job = add_job(db, src)
    r = client.post(f"/poslovi/{job.id}/poslodavac", data={"phone": "12ab"}, headers=ORIGIN)
    assert r.status_code == 200 and "data-error-summary" in r.text
    field = re.search(r'<input id="f-phone"[^>]*>', r.text, re.S).group(0)
    assert 'aria-describedby="f-phone-err f-phone-hint"' in field and 'aria-invalid="true"' in field
    clean = client.get(f"/poslovi/{job.id}").text
    assert 'aria-describedby="f-phone-hint"' in clean and "aria-invalid" not in clean
