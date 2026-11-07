"""Step 4.4: the Dnevnik page, the app's log in plain Croatian."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import event_log
from app.core.db import get_session
from app.core.event_log import load_log, parse_group, parse_page
from app.main import create_app
from app.models import Event, Job, Source

T0 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)  # 12:00 in Croatia


def make_source(db, key="sp", name="studentski-poslovi.hr"):
    src = Source(key=key, name=name)
    db.add(src)
    db.flush()
    return src


def make_job(db, src, n=1, title=None):
    job = Job(source=src, external_id=f"e{n}", url="https://example.hr/x", first_seen_at=T0,
              title=title or f"Posao {n}")
    db.add(job)
    db.flush()
    return job


def log_event(db, kind, job=None, minutes=0, **data):
    event = Event(kind=kind, job_id=job.id if job else None, data=data or None,
                  ts=T0 + timedelta(minutes=minutes))
    db.add(event)
    db.flush()
    return event


def texts(db, group="sve", page=1):
    return [e.text for e in load_log(db, group, page).entries]


# ---------------------------------------------------------------- the wording
def test_every_kind_the_app_writes_has_plain_croatian(db):
    src = make_source(db)
    job = make_job(db, src, title="Konobar vikend")
    log_event(db, "collected", job, 1, score=83, excluded=False, channel="email")
    log_event(db, "collected", job, 2, score=0, excluded=True, channel="none")
    log_event(db, "duplicate", job, 3, of=9)
    log_event(db, "source_run", None, 4, source="sp", status="ok", error=None, new=5)
    log_event(db, "source_changed", None, 5, source="sp", enabled=False, interval=30, by="user")
    log_event(db, "source_run_requested", None, 6, source="sp", by="user")
    log_event(db, "job_ignored", job, 7, **{"from": "seen"})
    log_event(db, "job_snoozed", job, 8, until="2026-10-18T06:00:00+00:00", **{"from": "seen"})
    log_event(db, "job_applied", job, 9)
    log_event(db, "job_reopened", job, 10)
    log_event(db, "job_unsnoozed", job, 11)
    log_event(db, "company_info_saved", job, 12, employer="Luna d.o.o.",
              fields=["phone", "director_name"])
    log_event(db, "something_new", None, 13)
    assert texts(db) == [
        "Događaj: something_new.",
        "Spremili ste podatke o poslodavcu Luna d.o.o. (telefon, ime direktora).",
        "Odgođeni oglas „Konobar vikend“ je ponovno među otvorenima.",
        "Vratili ste oglas „Konobar vikend“ među otvorene.",
        "Označili ste da ste se prijavili na oglas „Konobar vikend“.",
        "Odgodili ste oglas „Konobar vikend“ do 18.10.2026. u 08:00.",
        "Zanemarili ste oglas „Konobar vikend“.",
        "Vi ste pokrenuli provjeru izvora studentski-poslovi.hr.",
        "Vi ste promijenili izvor studentski-poslovi.hr: isključen, provjera svakih 30 min.",
        "Provjera izvora studentski-poslovi.hr: u redu, novih oglasa: 5.",
        "Oglas „Konobar vikend“ je duplikat drugog oglasa i preskočen je.",
        "Novi oglas „Konobar vikend“: ocjena 0, bez kontakta, ne odgovara vašem profilu.",
        "Novi oglas „Konobar vikend“: ocjena 83, e-mail.",
    ]


@pytest.mark.parametrize("status, level", [("ok", "ok"), ("partial", "warn"), ("error", "warn"),
                                           ("blocked", "bad"), ("parse_error", "bad")])
def test_a_source_run_is_marked_by_how_it_went(db, status, level):
    make_source(db)
    log_event(db, "source_run", None, 0, source="sp", status=status, error="oops", new=0)
    [entry] = load_log(db).entries
    assert entry.level == level


def test_the_error_message_is_included_but_cut(db):
    make_source(db)
    log_event(db, "source_run", None, 0, source="sp", status="error", new=0,
              error="timeout " + "x" * 400)
    [entry] = load_log(db).entries
    assert "(timeout xxx" in entry.text and entry.text.endswith(" …)") and len(entry.text) < 330


def test_an_unknown_source_key_and_odd_data_do_not_break_the_page(db):
    log_event(db, "source_run", None, 0, source="gone", status="mystery", new=None)
    log_event(db, "job_snoozed", None, 1, until="not a date")
    log_event(db, "company_info_saved", None, 2, employer="X", fields=["weird"])
    log_event(db, "collected", None, 3)
    out = texts(db)
    assert out[3].startswith("Provjera izvora gone: mystery")
    assert out[2] == "Odgodili ste oglas ."  # no ad and no time: still a sentence
    assert "(weird)" in out[1]
    assert out[0].startswith("Novi oglas")


def test_personal_details_never_reach_the_log_text(db):
    src = make_source(db)
    job = make_job(db, src)
    log_event(db, "company_info_saved", job, 0, employer="Luna d.o.o.", fields=["phone"])
    assert "+385" not in texts(db)[0] and "Horvat" not in texts(db)[0]


# ---------------------------------------------------------------- order, links, time
def test_newest_first_with_local_time_and_a_link_only_for_ads_that_exist(db):
    src = make_source(db)
    job = make_job(db, src)
    log_event(db, "job_ignored", job, 0)
    log_event(db, "job_applied", None, 1)
    ghost = Event(kind="job_reopened", job_id=None, ts=T0 + timedelta(minutes=2))
    db.add(ghost)
    db.flush()
    entries = load_log(db).entries
    assert [e.text.split()[0] for e in entries] == ["Vratili", "Označili", "Zanemarili"]
    assert entries[2].when == "17.10.2026. u 12:00" and entries[2].job_id == job.id
    assert entries[1].job_id is None and entries[0].job_id is None


# ---------------------------------------------------------------- groups and pages
def seed_mixed(db):
    src = make_source(db)
    job = make_job(db, src)
    log_event(db, "collected", job, 0, score=70, excluded=False, channel="email")
    log_event(db, "source_run", None, 1, source="sp", status="ok", new=1)
    log_event(db, "source_run", None, 2, source="sp", status="parse_error", error="x", new=0)
    log_event(db, "job_ignored", job, 3)
    log_event(db, "source_changed", None, 4, source="sp", enabled=True, interval=10, by="user")


def test_groups(db):
    seed_mixed(db)
    assert len(texts(db, "sve")) == 5
    assert [t.split()[0] for t in texts(db, "oglasi")] == ["Novi"]
    assert [t.split()[0] for t in texts(db, "radnje")] == ["Zanemarili"]
    assert len(texts(db, "izvori")) == 3
    problems = texts(db, "problemi")
    assert len(problems) == 1 and "izvor izgleda pokvaren" in problems[0]


def test_group_and_page_values_from_the_address():
    assert parse_group("radnje") == "radnje"
    assert parse_group("<script>") == "sve" and parse_group(None) == "sve"
    pages = [parse_page(raw) for raw in ("3", "0", "-4", "x", None)]
    assert pages == [3, 1, 1, 1, 1]
    assert parse_page("99999999999999999999") == 100000
    assert parse_page("²") == 1


def test_paging(db, monkeypatch):
    monkeypatch.setattr(event_log, "PAGE_SIZE", 2)
    src = make_source(db)
    for i in range(5):
        log_event(db, "job_applied", make_job(db, src, i, title=f"J{i}"), i)
    first = load_log(db, "sve", 1)
    assert [e.job_title for e in first.entries] == ["J4", "J3"] and first.pages == 3
    assert [e.job_title for e in load_log(db, "sve", 3).entries] == ["J0"]
    assert load_log(db, "sve", 99).page == 3  # too far: the last page
    assert load_log(db).total == 5


# ---------------------------------------------------------------- the page
@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    return TestClient(app, follow_redirects=False)


def test_empty_log_says_so(client):
    r = client.get("/dnevnik")
    assert r.status_code == 200 and "<h1>Dnevnik</h1>" in r.text and "Dnevnik je prazan." in r.text
    assert '<a href="/dnevnik" aria-current="page">Dnevnik</a>' in r.text


def test_page_lists_the_events_with_links_and_warning_labels(client, db):
    seed_mixed(db)
    html = client.get("/dnevnik").text
    assert "Provjera izvora studentski-poslovi.hr: izvor izgleda pokvaren" in html
    assert '<span class="badge badge--bad">Problem</span>' in html
    assert 'href="/poslovi/1">Otvori oglas<span class="visually-hidden"> ' in html
    assert "17.10.2026. u 12:00" in html


def test_filter_and_odd_addresses(client, db):
    seed_mixed(db)
    only_problems = client.get("/dnevnik?skupina=problemi").text
    assert only_problems.count('class="log__item') == 1 and "Poništi" in only_problems
    assert '<option value="problemi" selected>Problemi</option>' in only_problems
    for query in ("?skupina=<script>", "?stranica=abc", "?skupina=oglasi&stranica=99999"):
        assert client.get("/dnevnik" + query).status_code == 200
    none = client.get("/dnevnik?skupina=radnje").text
    assert none.count('class="log__item') == 1
    db.query(Event).delete()
    assert "Nema događaja u ovoj skupini." in client.get("/dnevnik?skupina=radnje").text


def test_titles_from_ads_are_escaped(client, db):
    src = make_source(db)
    job = make_job(db, src, title="<script>alert(1)</script>")
    log_event(db, "job_ignored", job, 0)
    html = client.get("/dnevnik").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_paging_links_keep_the_group(client, db, monkeypatch):
    monkeypatch.setattr(event_log, "PAGE_SIZE", 2)
    src = make_source(db)
    for i in range(5):
        log_event(db, "job_applied", make_job(db, src, i), i)
    page1 = client.get("/dnevnik?skupina=radnje").text
    assert "Stranica 1 od 3" in page1 and "Noviji" not in page1
    assert 'href="/dnevnik?skupina=radnje&amp;stranica=2">Stariji</a>' in page1
    page3 = client.get("/dnevnik?skupina=radnje&stranica=3").text
    assert "Stariji" not in page3
    assert 'href="/dnevnik?skupina=radnje&amp;stranica=2">Noviji</a>' in page3
    plain = client.get("/dnevnik?stranica=2").text
    assert 'href="/dnevnik">Noviji</a>' in plain
    assert 'href="/dnevnik?stranica=3">Stariji</a>' in plain


def test_reading_the_log_changes_nothing(client, db):
    seed_mixed(db)
    before = db.query(Event).count()
    client.get("/dnevnik")
    assert db.query(Event).count() == before


def test_the_log_is_ordered_by_time_not_by_when_it_was_written(db):
    older_written_last = log_event(db, "job_applied", None, minutes=-60)
    newer_written_first = Event(kind="job_reopened", ts=T0 + timedelta(minutes=5))
    db.add(newer_written_first)
    db.flush()
    assert older_written_last.id > 0 and newer_written_first.id > older_written_last.id
    assert [t.split()[0] for t in texts(db)] == ["Vratili", "Označili"]
    db.add(Event(kind="job_ignored", ts=T0 + timedelta(minutes=5)))  # same time: the later id first
    db.flush()
    assert texts(db)[0].startswith("Zanemarili")
