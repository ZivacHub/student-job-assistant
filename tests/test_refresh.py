"""Plan 4.8: the "Osvježi" button on the Inbox checks the sources for new ads now."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.refresh_view import back_address, refresh_notice, refresh_outcome
from app.core.sources_view import (
    MIN_GAP,
    RefreshResult,
    RefreshState,
    new_ads_phrase,
    parse_since,
    refresh_all,
    refresh_state,
    start_run,
)
from app.main import create_app
from app.models import Event, Job, Source
from app.models.base import utcnow
from tests.test_sources_page import FakeScheduler, make_source

ORIGIN = {"origin": "http://testserver"}
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def add_job(db, src, n, **kw):
    values = dict(source=src, external_id=f"e{n}", url=f"https://example.hr/{n}",
                  title=f"Posao {n}", status="seen", first_seen_at=NOW)
    values.update(kw)
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


# ---------------------------------------------------------------- small words
@pytest.mark.parametrize("n, text", [
    (0, "0 novih oglasa"), (1, "1 novi oglas"), (2, "2 nova oglasa"), (4, "4 nova oglasa"),
    (5, "5 novih oglasa"), (11, "11 novih oglasa"), (12, "12 novih oglasa"),
    (14, "14 novih oglasa"), (21, "21 novi oglas"), (22, "22 nova oglasa"),
    (101, "101 novi oglas"), (111, "111 novih oglasa"),
])
def test_new_ads_in_croatian(n, text):
    assert new_ads_phrase(n) == text


def test_the_start_of_a_refresh_is_read_from_the_address_with_care():
    stamp = int((NOW - timedelta(minutes=2)).timestamp())
    assert parse_since(str(stamp), NOW) == NOW - timedelta(minutes=2)
    assert parse_since(str(int((NOW + timedelta(seconds=30)).timestamp())), NOW) is not None
    for bad in (None, "", "abc", "1.5", "-1", "0", "99999999999999999999",
                str(int((NOW + timedelta(hours=1)).timestamp())),      # in the future
                str(int((NOW - timedelta(days=2)).timestamp())),       # too old
                "²"):
        assert parse_since(bad, NOW) is None, bad


def test_back_address_keeps_filters_and_adds_parameters():
    assert back_address("") == "/poslovi"
    assert back_address("", ok="osvjezeno") == "/poslovi?ok=osvjezeno"
    assert back_address("?prikaz=sve&kanal=email", ok="x", od=5) == (
        "/poslovi?prikaz=sve&kanal=email&ok=x&od=5")


def test_what_the_button_did_becomes_a_key():
    assert refresh_outcome(RefreshResult([], [], [], no_scheduler=True)) == "bez_rasporeda"
    assert refresh_outcome(RefreshResult([], [], [], no_sources=True)) == "nema_izvora"
    assert refresh_outcome(RefreshResult(["a"], ["b"], [])) == "osvjezeno"
    assert refresh_outcome(RefreshResult([], ["b"], ["c"])) == "preskoceno"


def test_the_messages():
    running = RefreshState(running=True, last_check=None, new_ads=0)
    done = lambda n: RefreshState(running=False, last_check="prije 1 min", new_ads=n)  # noqa: E731
    assert "u tijeku" in refresh_notice("osvjezeno", running)
    assert refresh_notice("osvjezeno", done(0)) == "Provjera je gotova: nema novih oglasa."
    assert refresh_notice("osvjezeno", done(3)) == "Provjera je gotova: 3 nova oglasa."
    assert refresh_notice("osvjezeno", done(None)) == "Provjera je gotova."
    assert "prije manje od minute" in refresh_notice("preskoceno", done(None))
    assert "nije uključeno u ovom pokretanju" in refresh_notice("bez_rasporeda", done(None))
    assert "Nema uključenih izvora" in refresh_notice("nema_izvora", done(None))
    assert refresh_notice("nonsense", done(1)) is None and refresh_notice(None, done(1)) is None


# ---------------------------------------------------------------- not twice in a minute
def test_a_source_checked_a_moment_ago_is_not_asked_again(db):
    make_source(db, last_run_at=NOW - MIN_GAP + timedelta(seconds=1))
    sched = FakeScheduler()
    assert start_run(db, sched, "sp", NOW) == "recent"
    assert sched.calls == [] and db.query(Event).count() == 0


def test_after_a_minute_it_may_run_again_and_naive_times_work(db):
    make_source(db, last_run_at=(NOW - MIN_GAP - timedelta(seconds=1)).replace(tzinfo=None))
    sched = FakeScheduler()
    assert start_run(db, sched, "sp", NOW) == "started"
    assert sched.ran.wait(5)


def test_busy_beats_recent_and_off_beats_busy(db):
    make_source(db, last_run_at=NOW)
    sched = FakeScheduler()
    sched.busy.add("sp")
    assert start_run(db, sched, "sp", NOW) == "busy"
    db.query(Source).one().enabled = False
    assert start_run(db, sched, "sp", NOW) == "off"


# ---------------------------------------------------------------- refresh everything
def seed_sources(db):
    make_source(db, "a", "Prvi", last_run_at=NOW - timedelta(minutes=10))
    make_source(db, "b", "Drugi", last_run_at=NOW - timedelta(seconds=20))  # just checked
    make_source(db, "c", "Treći", last_run_at=NOW - timedelta(minutes=10))  # busy
    make_source(db, "d", "Isključen", enabled=False)
    make_source(db, "paste", "Pasted ads", interval_minutes=0)


def test_refresh_asks_only_the_sources_that_can_be_asked(db):
    seed_sources(db)
    sched = FakeScheduler()
    sched.busy.add("c")
    result = refresh_all(db, sched, NOW)
    assert (result.started, result.recent, result.busy) == (["Prvi"], ["Drugi"], ["Treći"])
    assert result.no_scheduler is False and result.no_sources is False
    assert sched.ran.wait(5) and sched.calls == ["a"]
    assert [e.data["source"] for e in db.query(Event)] == ["a"]


def test_refresh_without_a_scheduler_says_so_and_starts_nothing(db):
    seed_sources(db)
    result = refresh_all(db, None, NOW)
    assert result.no_scheduler and result.started == [] and db.query(Event).count() == 0


def test_refresh_with_no_sources_to_ask(db):
    make_source(db, "d", "Isključen", enabled=False)
    make_source(db, "paste", "Pasted ads", interval_minutes=0)
    assert refresh_all(db, FakeScheduler(), NOW).no_sources is True


def test_the_state_after_a_refresh(db):
    src = make_source(db, "a", "Prvi", last_run_at=NOW - timedelta(minutes=4))
    make_source(db, "b", "Drugi", last_run_at=NOW - timedelta(minutes=9))
    since = NOW - timedelta(minutes=2)
    add_job(db, src, 1, first_seen_at=NOW - timedelta(minutes=1))                    # counted
    add_job(db, src, 2, first_seen_at=NOW - timedelta(minutes=1), status="new")      # counted
    add_job(db, src, 3, first_seen_at=NOW - timedelta(minutes=1), status="ignored")  # set aside
    add_job(db, src, 4, first_seen_at=NOW - timedelta(minutes=30))                   # old
    add_job(db, src, 5, first_seen_at=NOW - timedelta(minutes=1), duplicate_of=1)    # duplicate
    sched = FakeScheduler()
    state = refresh_state(db, sched, since, NOW)
    assert (state.running, state.last_check, state.new_ads) == (False, "prije 4 min", 2)
    sched.busy.add("b")
    assert refresh_state(db, sched, None, NOW).running is True
    assert refresh_state(db, sched, None, NOW).new_ads is None
    assert refresh_state(db, None, None, NOW).running is False


def test_the_state_with_nothing_checked_yet(db):
    make_source(db)
    state = refresh_state(db, FakeScheduler(), None, NOW)
    assert state.last_check is None and state.running is False


# ---------------------------------------------------------------- the page
@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    return TestClient(app, follow_redirects=False)


def test_the_inbox_has_the_button(client):
    html = client.get("/poslovi").text
    assert '<form method="post" action="/poslovi/osvjezi">' in html
    assert ">Osvježi</button>" in html and "Izvori još nisu provjereni." in html
    assert 'type="hidden"' not in html.split('action="/poslovi/osvjezi"')[1].split("</form>")[0]
    assert 'http-equiv="refresh"' not in html


def test_the_button_remembers_the_filters(client):
    html = client.get("/poslovi?prikaz=sve&kanal=phone&ocjena=60").text
    form = html.split('action="/poslovi/osvjezi"')[1].split("</form>")[0]
    for hidden in ('name="prikaz" value="sve"', 'name="kanal" value="phone"',
                   'name="ocjena" value="60"'):
        assert hidden in form


def test_last_check_line(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(minutes=4))
    assert "Zadnja provjera izvora: prije 4 min." in client.get("/poslovi").text


def test_while_a_source_checks_the_button_waits_and_the_page_reloads_itself(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(minutes=4))
    sched = FakeScheduler()
    sched.busy.add("sp")
    client.app.state.scheduler = sched
    html = client.get("/poslovi").text
    assert 'http-equiv="refresh" content="10"' in html
    assert "Provjera izvora je u tijeku…" in html
    assert "disabled>Provjera u tijeku…</button>" in html


def test_pressing_the_button_starts_every_checkable_source_and_comes_back(client, db):
    make_source(db, "a", "Prvi", last_run_at=utcnow() - timedelta(minutes=10))
    make_source(db, "b", "Drugi", last_run_at=utcnow() - timedelta(minutes=10))
    sched = FakeScheduler()
    client.app.state.scheduler = sched
    r = client.post("/poslovi/osvjezi", headers=ORIGIN)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/poslovi?ok=osvjezeno&od=")
    assert int(location.split("od=")[1]) > 1_700_000_000
    assert sched.ran.wait(5)
    for _ in range(50):
        if sorted(sched.calls) == ["a", "b"]:
            break
        sched.ran.wait(0.1)
    assert sorted(sched.calls) == ["a", "b"]
    assert db.query(Event).filter_by(kind="source_run_requested").count() == 2


def test_the_whole_round_trip_shows_the_result(client, db):
    src = make_source(db, last_run_at=utcnow() - timedelta(minutes=10))
    client.app.state.scheduler = FakeScheduler()
    location = client.post("/poslovi/osvjezi", headers=ORIGIN).headers["location"]
    for n in (1, 2):  # the checker "finds" two ads while the page is loading
        add_job(db, src, n, first_seen_at=utcnow())
    page = client.get(location).text
    assert "<p>Provjera je gotova: 2 nova oglasa.</p>" in page and 'role="status"' in page
    assert "Posao 1" in page and "Posao 2" in page


def test_the_button_keeps_the_filters_in_the_way_back(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(minutes=10))
    client.app.state.scheduler = FakeScheduler()
    r = client.post("/poslovi/osvjezi", headers=ORIGIN,
                    data={"prikaz": "sve", "kanal": "email", "ocjena": "60"})
    expected = "/poslovi?prikaz=sve&kanal=email&ocjena=60&ok=osvjezeno"
    assert r.headers["location"].startswith(expected)


def test_odd_filter_values_in_the_button_are_dropped(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(minutes=10))
    client.app.state.scheduler = FakeScheduler()
    r = client.post("/poslovi/osvjezi", headers=ORIGIN, data={
        "prikaz": "<script>", "kanal": "http://evil.example/", "ocjena": "9999"})
    location = r.headers["location"]
    assert location.startswith("/poslovi?ocjena=100&ok=osvjezeno")
    assert "evil" not in location and "script" not in location


def test_without_a_scheduler_the_list_is_reloaded_with_an_explanation(client, db):
    make_source(db)
    r = client.post("/poslovi/osvjezi", headers=ORIGIN)
    assert r.headers["location"].startswith("/poslovi?ok=bez_rasporeda&od=")
    assert "nije uključeno u ovom pokretanju" in client.get(r.headers["location"]).text


def test_pressing_twice_quickly_does_not_ask_the_sites_again(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(seconds=10))
    sched = FakeScheduler()
    client.app.state.scheduler = sched
    r = client.post("/poslovi/osvjezi", headers=ORIGIN)
    assert r.headers["location"].startswith("/poslovi?ok=preskoceno")
    assert sched.calls == []
    assert "prije manje od minute" in client.get(r.headers["location"]).text


def test_nothing_to_check(client, db):
    make_source(db, enabled=False)
    client.app.state.scheduler = FakeScheduler()
    r = client.post("/poslovi/osvjezi", headers=ORIGIN)
    assert r.headers["location"].startswith("/poslovi?ok=nema_izvora")
    assert "Nema uključenih izvora" in client.get(r.headers["location"]).text


@pytest.mark.parametrize("query", ["?ok=osvjezeno", "?ok=osvjezeno&od=abc", "?ok=osvjezeno&od=1",
                                   "?ok=<script>", "?od=5", "?ok=osvjezeno&od=%C2%B2"])
def test_odd_addresses_still_give_a_normal_page(client, query):
    assert client.get("/poslovi" + query).status_code == 200


def test_a_message_never_comes_from_the_address(client):
    html = client.get("/poslovi?ok=<b>hej</b>").text
    assert "<b>hej</b>" not in html and 'alert--ok' not in html


def test_the_button_only_works_from_this_app(client, db):
    make_source(db)
    sched = FakeScheduler()
    client.app.state.scheduler = sched
    assert client.post("/poslovi/osvjezi").status_code == 403
    assert client.post("/poslovi/osvjezi", headers={"origin": "http://evil.example"}
                       ).status_code == 403
    assert sched.calls == []


def test_the_izvori_page_explains_a_too_early_press(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(seconds=5))
    client.app.state.scheduler = FakeScheduler()
    r = client.post("/izvori/sp/pokreni", headers=ORIGIN)
    assert r.status_code == 200 and "provjeren prije manje od minute" in r.text


# ---------------------------------------------------------------- a source this program cannot read
def test_a_source_without_a_checker_is_labelled_and_never_asked(db):
    make_source(db, "a", "Ima čitač", last_run_at=NOW - timedelta(minutes=10))
    make_source(db, "x", "Nema čitača u kodu", last_run_at=NOW - timedelta(minutes=10))
    sched = FakeScheduler(known={"a"})
    from app.core.sources_view import load_sources

    rows = {r.key: r for r in load_sources(db, sched, NOW)}
    assert rows["a"].runnable and rows["x"].runnable is False
    assert rows["x"].state_label == "Nema čitača" and "nema čitač" in rows["x"].explanation
    assert rows["x"].next_run is None
    assert start_run(db, sched, "x", NOW) == "no_collector" and sched.calls == []
    result = refresh_all(db, sched, NOW)
    assert result.started == ["Ima čitač"] and result.no_sources is False
    assert sched.ran.wait(5) and sched.calls == ["a"]


def test_only_sources_without_checkers_means_nothing_to_ask(db):
    make_source(db, "x", "Nema čitača")
    sched = FakeScheduler(known=set())
    assert refresh_all(db, sched, NOW).no_sources is True and sched.calls == []
    state = refresh_state(db, sched, None, NOW)
    assert state.running is False


def test_without_a_scheduler_no_source_is_judged(db):
    make_source(db, "x", "Bilo koji")
    from app.core.sources_view import load_sources

    assert load_sources(db, None, NOW)[0].runnable is True


def test_the_page_explains_a_source_without_a_checker(client, db):
    make_source(db, "x", "Stari izvor")
    client.app.state.scheduler = FakeScheduler(known=set())
    html = client.get("/izvori").text
    assert "Nema čitača" in html and "Ova verzija programa nema čitač" in html
    assert "disabled" in html.split('action="/izvori/x/pokreni"')[1].split("</form>")[0]
    r = client.post("/izvori/x/pokreni", headers=ORIGIN)
    assert r.status_code == 200 and "nema čitač za taj izvor" in r.text


def test_a_source_without_a_checker_is_not_reported_as_broken(client, db):
    make_source(db, "x", "Stari izvor", last_status="blocked", consecutive_failures=2)
    assert "ne radi kako treba" in client.get("/poslovi").text  # no scheduler: judged by its record
    client.app.state.scheduler = FakeScheduler(known=set())
    assert "ne radi kako treba" not in client.get("/poslovi").text


# ---------------------------------------------------------------- a reload can be stopped
def test_the_reload_can_be_stopped_and_resumed(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(minutes=4))
    sched = FakeScheduler()
    sched.busy.add("sp")
    client.app.state.scheduler = sched
    running = client.get("/poslovi?ok=osvjezeno").text
    assert 'http-equiv="refresh"' in running
    assert 'href="/poslovi?ok=osvjezeno&amp;auto=0">Zaustavi automatsko osvježavanje</a>' in running
    stopped = client.get("/poslovi?ok=osvjezeno&auto=0").text
    assert 'http-equiv="refresh"' not in stopped
    assert "Automatsko osvježavanje je zaustavljeno" in stopped
    assert 'href="/poslovi?ok=osvjezeno">Osvježi stranicu</a>' in stopped
    assert "Zaustavi automatsko" not in stopped


def test_no_stop_link_when_nothing_reloads(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(minutes=4))
    client.app.state.scheduler = FakeScheduler()
    html = client.get("/poslovi?ok=osvjezeno").text
    assert "Zaustavi automatsko" not in html and 'http-equiv="refresh"' not in html


def test_the_izvori_reload_can_be_stopped_too(client, db):
    make_source(db, last_run_at=utcnow() - timedelta(minutes=4))
    sched = FakeScheduler()
    sched.busy.add("sp")
    client.app.state.scheduler = sched
    html = client.get("/izvori").text
    assert 'http-equiv="refresh"' in html
    assert 'href="/izvori?auto=0">Zaustavi automatsko osvježavanje</a>' in html
    stopped = client.get("/izvori?auto=0").text
    assert 'http-equiv="refresh"' not in stopped and "Zaustavi automatsko" not in stopped
