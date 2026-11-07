"""Step 4.4 / 2.8: the Izvori page: how each source is doing, switching it, running it now."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.sources_view import (
    SettingsError,
    broken_sources,
    describe_future,
    load_sources,
    save_source_settings,
    start_run,
)
from app.main import create_app
from app.models import Event, Job, Source
from app.models.base import utcnow

ORIGIN = {"origin": "http://testserver"}
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


class FakeScheduler:
    """Stands in for the real scheduler: records what the page asks it to do."""

    running = True

    def __init__(self, known=None):
        self.known = known  # keys it has a checker for (None = all)
        self.calls: list[str] = []
        self.synced: list[str] = []
        self.busy: set[str] = set()
        self.next: dict[str, datetime] = {}
        self.ran = threading.Event()

    def knows(self, key):
        return self.known is None or key in self.known

    def is_running(self, key):
        return key in self.busy

    def next_run(self, key):
        return self.next.get(key)

    def sync_source(self, key):
        self.synced.append(key)

    def run_source(self, key):
        self.calls.append(key)
        self.ran.set()
        return "ran"


def make_source(db, key="sp", name="studentski-poslovi.hr", **kw):
    values = dict(key=key, name=name, interval_minutes=10, enabled=True)
    values.update(kw)
    src = Source(**values)
    db.add(src)
    db.flush()
    return src


def add_job(db, src, n, **kw):
    job = Job(source=src, external_id=f"e{n}", url=f"https://example.hr/{n}", title=f"Posao {n}",
              first_seen_at=kw.pop("first_seen_at", NOW), **kw)
    db.add(job)
    db.flush()
    return job


def row_of(db, key, scheduler=None, now=NOW):
    return next(r for r in load_sources(db, scheduler, now) if r.key == key)


# ---------------------------------------------------------------- how a source is described
@pytest.mark.parametrize("status, state, label", [
    (None, "new", "Još nije pokrenuto"),
    ("ok", "ok", "U redu"),
    ("partial", "warn", "Djelomično"),
    ("error", "warn", "Greška"),
    ("blocked", "bad", "Pristup odbijen"),
    ("parse_error", "bad", "Izvor izgleda pokvaren"),
])
def test_status_in_plain_words(db, status, state, label):
    make_source(db, last_status=status, last_run_at=NOW - timedelta(minutes=5), last_items=3)
    r = row_of(db, "sp")
    assert (r.state, r.state_label) == (state, label)
    assert (r.last_run, r.last_items) == ("prije 5 min", 3)


def test_a_broken_source_explains_itself(db):
    make_source(db, last_status="parse_error", last_error="no ad cards found")
    r = row_of(db, "sp")
    assert "izgleda drugačije nego prije" in r.explanation and r.error == "no ad cards found"


def test_an_ok_source_shows_no_old_error(db):
    make_source(db, last_status="ok", last_error="stale text")
    assert row_of(db, "sp").error is None


def test_a_long_error_is_cut_and_whitespace_is_tidied(db):
    make_source(db, last_status="error", last_error="line one\n\n" + "x" * 500)
    error = row_of(db, "sp").error
    assert len(error) <= 305 and error.endswith("…") and "\n" not in error


def test_switched_off_and_switched_off_by_the_program(db):
    make_source(db, "a", "Ručno isključen", enabled=False, last_status="ok")
    make_source(db, "b", "Odbijen", enabled=False, last_status="blocked", consecutive_failures=3)
    a, b = row_of(db, "a"), row_of(db, "b")
    assert (a.state, a.state_label, a.explanation, a.auto_disabled) == (
        "off", "Isključeno", None, False)
    assert b.auto_disabled and "3 puta odbila pristup" in b.explanation


def test_the_pasted_ads_source_is_manual(db):
    make_source(db, "paste", "Pasted ads", interval_minutes=0)
    r = row_of(db, "paste")
    assert r.manual and r.state_label == "Ručno dodavanje" and r.next_run is None


def test_a_running_source_says_so_and_has_no_next_time(db):
    make_source(db, last_status="ok")
    sched = FakeScheduler()
    sched.busy.add("sp")
    sched.next["sp"] = NOW + timedelta(minutes=3)
    r = row_of(db, "sp", sched)
    assert r.running and r.state == "run" and r.next_run is None


def test_next_run_comes_from_the_scheduler(db):
    make_source(db, last_status="ok")
    sched = FakeScheduler()
    sched.next["sp"] = NOW + timedelta(minutes=7, seconds=20)
    assert row_of(db, "sp", sched).next_run == "za 7 min"
    assert row_of(db, "sp", None).next_run is None


def test_first_fill_note_only_until_one_clean_run(db):
    make_source(db, last_status="ok")
    assert row_of(db, "sp").first_fill_pending is True
    db.query(Source).one().baselined_at = NOW
    assert row_of(db, "sp").first_fill_pending is False


def test_ad_counts_skip_duplicates_and_count_the_last_day(db):
    src = make_source(db)
    first = add_job(db, src, 1, first_seen_at=NOW - timedelta(hours=1))
    add_job(db, src, 2, first_seen_at=NOW - timedelta(hours=30))
    add_job(db, src, 3, first_seen_at=NOW - timedelta(hours=2), duplicate_of=first.id)
    r = row_of(db, "sp")
    assert (r.total_ads, r.ads_24h) == (2, 1)


@pytest.mark.parametrize("minutes, text", [(0, "za manje od minute"), (7, "za 7 min"),
                                           (59, "za 59 min"), (120, "za 2 h")])
def test_describe_future(minutes, text):
    assert describe_future(NOW + timedelta(minutes=minutes, seconds=10), NOW) == text


def test_describe_future_handles_missing_and_naive():
    assert describe_future(None, NOW) is None
    assert describe_future((NOW + timedelta(minutes=5, seconds=1)).replace(tzinfo=None),
                           NOW) == "za 5 min"


# ---------------------------------------------------------------- the warning on the Inbox
def test_who_counts_as_broken(db):
    make_source(db, "ok", "Dobar", last_status="ok")
    make_source(db, "once", "Jednom pao", last_status="error", consecutive_failures=1)
    make_source(db, "twice", "Dvaput pao", last_status="error", consecutive_failures=2)
    make_source(db, "blocked", "Blokiran", last_status="blocked", consecutive_failures=1)
    make_source(db, "parse", "Pokvaren", last_status="parse_error", consecutive_failures=1)
    make_source(db, "off", "Ručno isključen", enabled=False, last_status="error",
                consecutive_failures=5)
    make_source(db, "auto", "Sam isključen", enabled=False, last_status="blocked",
                consecutive_failures=3)
    make_source(db, "paste", "Pasted", interval_minutes=0, last_status="error",
                consecutive_failures=9)
    assert broken_sources(db) == ["Dvaput pao", "Blokiran", "Pokvaren", "Sam isključen"]


# ---------------------------------------------------------------- changing a source
def test_changing_a_source_is_logged(db):
    make_source(db)
    save_source_settings(db, "sp", enabled=False, interval=30)
    src = db.query(Source).one()
    assert (src.enabled, src.interval_minutes) == (False, 30)
    [event] = db.query(Event).all()
    assert (event.kind, event.data) == (
        "source_changed", {"source": "sp", "enabled": False, "interval": 30, "by": "user"})


def test_switching_a_source_back_on_gives_it_a_fresh_start(db):
    make_source(db, enabled=False, last_status="blocked", consecutive_failures=3)
    save_source_settings(db, "sp", enabled=True, interval=10)
    assert db.query(Source).one().consecutive_failures == 0


def test_only_offered_intervals_unknown_and_manual_sources_are_refused(db):
    make_source(db)
    make_source(db, "paste", "Pasted", interval_minutes=0)
    for key, interval in (("sp", 7), ("sp", 0), ("sp", -5), ("nope", 10), ("paste", 10)):
        with pytest.raises(SettingsError):
            save_source_settings(db, key, enabled=True, interval=interval)
    assert db.query(Event).count() == 0
    assert db.query(Source).filter_by(key="sp").one().interval_minutes == 10


# ---------------------------------------------------------------- run now
def test_run_now_starts_the_scheduler_in_the_background_and_logs(db):
    make_source(db)
    sched = FakeScheduler()
    assert start_run(db, sched, "sp") == "started"
    assert sched.ran.wait(5) and sched.calls == ["sp"]
    assert [e.kind for e in db.query(Event)] == ["source_run_requested"]


@pytest.mark.parametrize("setup, expected", [
    ("unknown", "unknown"), ("manual", "unknown"), ("no_scheduler", "no_scheduler"),
    ("off", "off"), ("busy", "busy"),
])
def test_run_now_refusals_start_nothing(db, setup, expected):
    make_source(db, enabled=setup != "off", interval_minutes=0 if setup == "manual" else 10)
    sched = None if setup == "no_scheduler" else FakeScheduler()
    if setup == "busy":
        sched.busy.add("sp")
    assert start_run(db, sched, "nope" if setup == "unknown" else "sp") == expected
    if sched is not None:
        assert sched.calls == []
    assert db.query(Event).count() == 0


# ---------------------------------------------------------------- the page
@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    return TestClient(app, follow_redirects=False)


def test_empty_page_says_what_is_going_on(client):
    r = client.get("/izvori")
    assert r.status_code == 200 and "<h1>Izvori</h1>" in r.text
    assert "Još nema izvora." in r.text


def test_state_block_tells_the_safety_facts(client):
    html = client.get("/izvori").text
    assert "Stanje programa" in html
    assert "<strong>Isključeno.</strong> Ništa se ne šalje poslodavcima" in html
    assert "<strong>Nije postavljeno.</strong>" in html  # Gmail
    assert "Samo ovo računalo" in html and 'href="/mjesta"' in html
    assert "<strong>Nije uključeno</strong> u ovom pokretanju" in html


def test_state_block_with_a_scheduler(client):
    client.app.state.scheduler = FakeScheduler()
    assert "<strong>Radi</strong> dok je ovaj program pokrenut" in client.get("/izvori").text


def test_a_source_card_has_the_numbers_the_status_and_the_controls(client, db):
    src = make_source(db, last_status="ok", last_run_at=utcnow() - timedelta(minutes=4),
                      last_items=3, baselined_at=utcnow())
    add_job(db, src, 1, first_seen_at=utcnow() - timedelta(hours=1))
    add_job(db, src, 2, first_seen_at=utcnow() - timedelta(hours=40))
    html = client.get("/izvori").text
    for text in ("studentski-poslovi.hr", "U redu", "Zadnja provjera: <strong>prije 4 min</strong>",
                 "novih oglasa: <strong>3</strong>", "oglasa ukupno: <strong>2</strong>",
                 "(u zadnja 24 h: 1)", "Provjeri sada", "Uključeno", "Provjeravaj svakih",
                 '<option value="10" selected>10 min</option>'):
        assert text in html, text
    assert 'action="/izvori/sp/pokreni"' in html and 'action="/izvori/sp/postavke"' in html


def test_trouble_is_shown_with_the_message_from_the_site(client, db):
    make_source(db, last_status="parse_error", last_error="no ad cards found",
                last_run_at=utcnow(), consecutive_failures=1)
    html = client.get("/izvori").text
    assert "Izvor izgleda pokvaren" in html and "izgleda drugačije nego prije" in html
    assert "Poruka: no ad cards found" in html


def test_texts_from_sources_are_escaped(client, db):
    make_source(db, name='<img src=x onerror=alert(1)>', last_status="error",
                last_error="<script>alert(2)</script>", last_run_at=utcnow())
    html = client.get("/izvori").text
    assert "<img src=x" not in html and "<script>alert(2)" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_the_page_refreshes_itself_only_while_something_runs(client, db):
    make_source(db, last_status="ok")
    sched = FakeScheduler()
    client.app.state.scheduler = sched
    assert 'http-equiv="refresh"' not in client.get("/izvori").text
    sched.busy.add("sp")
    html = client.get("/izvori").text
    assert 'http-equiv="refresh" content="15"' in html and "Provjera u tijeku" in html
    assert "disabled" in html.split('action="/izvori/sp/pokreni"')[1].split("</form>")[0]


def test_a_switched_off_source_cannot_be_run(client, db):
    make_source(db, enabled=False)
    form = client.get("/izvori").text.split('action="/izvori/sp/pokreni"')[1].split("</form>")[0]
    assert "disabled" in form


def test_the_pasted_ads_source_offers_adding_not_settings(client, db):
    make_source(db, "paste", "Pasted ads", interval_minutes=0)
    html = client.get("/izvori").text
    assert 'href="/dodaj"' in html and "/izvori/paste/postavke" not in html
    assert "/izvori/paste/pokreni" not in html


def test_saving_settings(client, db):
    make_source(db)
    sched = FakeScheduler()
    client.app.state.scheduler = sched
    r = client.post("/izvori/sp/postavke", data={"interval": "30"}, headers=ORIGIN)  # box unticked
    assert r.status_code == 303 and r.headers["location"] == "/izvori?ok=spremljeno"
    src = db.query(Source).one()
    assert (src.enabled, src.interval_minutes) == (False, 30)
    assert sched.synced == ["sp"]
    assert "Postavke izvora su spremljene." in client.get(r.headers["location"]).text


def test_saving_with_the_box_ticked_switches_it_on(client, db):
    make_source(db, enabled=False)
    client.post("/izvori/sp/postavke", data={"enabled": "on", "interval": "15"}, headers=ORIGIN)
    assert db.query(Source).one().enabled is True


@pytest.mark.parametrize("interval", ["7", "abc", "", "-1", "99999999999999999999"])
def test_a_bad_interval_is_explained_and_nothing_changes(client, db, interval):
    make_source(db)
    sched = FakeScheduler()
    client.app.state.scheduler = sched
    r = client.post("/izvori/sp/postavke", data={"enabled": "on", "interval": interval},
                    headers=ORIGIN)
    assert r.status_code == 200 and 'role="alert"' in r.text and "nije ponuđen" in r.text
    assert db.query(Source).one().interval_minutes == 10 and sched.synced == []


def test_settings_for_a_missing_source_are_not_found(client):
    r = client.post("/izvori/nope/postavke", data={"interval": "10"}, headers=ORIGIN)
    assert r.status_code == 404 and "Izvor nije pronađen" in r.text and 'href="/izvori"' in r.text
    assert client.post("/izvori/nope/pokreni", headers=ORIGIN).status_code == 404


def test_run_now_button(client, db):
    make_source(db)
    sched = FakeScheduler()
    client.app.state.scheduler = sched
    r = client.post("/izvori/sp/pokreni", headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/izvori?ok=pokrenuto"
    assert sched.ran.wait(5) and sched.calls == ["sp"]
    assert "Provjera je pokrenuta." in client.get("/izvori?ok=pokrenuto").text


def test_run_now_when_it_cannot_run_explains_why(client, db):
    make_source(db)
    r = client.post("/izvori/sp/pokreni", headers=ORIGIN)  # no scheduler in this run
    assert r.status_code == 200 and "nije uključeno u ovom pokretanju" in r.text
    sched = FakeScheduler()
    sched.busy.add("sp")
    client.app.state.scheduler = sched
    r = client.post("/izvori/sp/pokreni", headers=ORIGIN)
    assert r.status_code == 200 and "već provjerava" in r.text and sched.calls == []


def test_posts_must_come_from_this_app(client, db):
    make_source(db)
    for path in ("/izvori/sp/postavke", "/izvori/sp/pokreni"):
        assert client.post(path, data={"interval": "30"}).status_code == 403
        assert client.post(path, data={"interval": "30"},
                           headers={"origin": "http://evil.example"}).status_code == 403
    assert db.query(Source).one().interval_minutes == 10


def test_the_inbox_warns_when_a_source_is_broken(client, db):
    make_source(db, name="Pokvaren izvor", last_status="parse_error", consecutive_failures=1)
    html = client.get("/poslovi").text
    assert "Izvor „Pokvaren izvor“ ne radi kako treba" in html and 'href="/izvori"' in html
    db.query(Source).one().last_status = "ok"
    assert "ne radi kako treba" not in client.get("/poslovi").text


def test_the_inbox_warning_for_several_sources(client, db):
    make_source(db, "a", "Prvi", last_status="blocked", consecutive_failures=1)
    make_source(db, "b", "Drugi", last_status="parse_error", consecutive_failures=1)
    assert "Izvori „Prvi“, „Drugi“ ne rade kako treba" in client.get("/poslovi").text


def test_navigation_leads_to_the_new_pages(client):
    html = client.get("/poslovi").text
    for href in ("/poslovi", "/izvori", "/dnevnik", "/mjesta"):
        assert f'<a href="{href}"' in html
    assert '<a href="/izvori" aria-current="page">Izvori</a>' in client.get("/izvori").text


def test_the_pasted_ads_source_has_a_croatian_name_everywhere(client, db):
    src = make_source(db, "paste", "Pasted ads", interval_minutes=0)
    job = add_job(db, src, 1, status="seen", channel="email", score=50)
    db.add(Event(kind="collected", job_id=job.id, data={"score": 50, "channel": "email"}))
    db.flush()
    assert row_of(db, "paste").name == "Ručno dodani oglasi"
    assert "Pasted ads" not in client.get("/izvori").text
    assert "Ručno dodani oglasi" in client.get("/poslovi").text
    assert "Pasted ads" not in client.get(f"/poslovi/{job.id}").text
