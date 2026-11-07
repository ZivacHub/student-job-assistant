"""Step 4.10 (D40): an ad that names only the town sits at the town centre, so no minutes may be
claimed for places inside that town ("TC Portanova" was '3 min' from home, really an hour on foot).
A named place in the title is looked up on the map; old ads are checked once. Nothing real is
contacted: the map search is a pretend server."""

import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.event_log import load_log
from app.core.inbox import build_row
from app.core.profile import get_or_create_profile
from app.geo.cities import lookup_city
from app.geo.nominatim import NOMINATIM_URL, Geocoder
from app.geo.placenames import place_names
from app.geo.recheck import make_locator, recheck_locations
from app.main import create_app
from app.models import Event, Job, Place, Source
from app.notify.alerts import reasons_hr
from app.scoring.engine import score_job
from app.sources.base import RawJob
from app.sources.ingest import ingest_raw
from tests.test_scheduler import FakeSource, factory, make  # noqa: F401  (factory is a fixture)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
OSIJEK = lookup_city("Osijek")
DAKOVO = lookup_city("Đakovo")
ZAGREB = lookup_city("Zagreb")
PORTANOVA = (45.5488, 18.7130)  # a made-up spot inside Osijek, about 1.5 km from the centre
TITLE = "Djelatnik u restoranu brze prehrane Surfer, TC Portanova"


def home(**kw) -> Place:
    base = dict(label="Dom", kind="home", lat=45.5520, lon=18.7050,
                max_minutes_by_mode={"walk": 30}, bonus_points=0, active=True)
    base.update(kw)
    return Place(**base)


def profile(db):
    p = get_or_create_profile(db)
    p.has_car, p.categories_wanted, p.weights = False, ["ugostiteljstvo"], {}
    return p


def job(precision="city", at=OSIJEK, **kw) -> Job:
    base = dict(title="Konobar", category="ugostiteljstvo", pay_eur_hour=8.0, channel="email",
                lat=at[0], lon=at[1], geo_precision=precision, city="Osijek",
                posted_at=NOW - timedelta(minutes=10))
    base.update(kw)
    return Job(**base)


def travel(result) -> dict:
    return next(r for r in result.reasons if r["k"] == "travel")


# ---------------------------------------------------------------- place names in a title
@pytest.mark.parametrize("title, names", [
    (TITLE, ("Portanova",)),
    ("Prodavač/ica, TC Portanova Osijek", ("Portanova Osijek", "Portanova")),
    ("Konobar u Avenue Mall", ("Avenue Mall",)),
    ("Radnik, Shopping centar Avenue", ("Avenue",)),
    ("Skladištar, Trgovački centar Dalmatinska, Osijek", ("Dalmatinska",)),
    ("Agent u call centru - Telesales", ()),
    ("Konobar/ica vikendom", ()),
    ("Pizza majstor ETC Osijek", ()),  # "ETC" is not "TC"
    ("", ()),
    (None, ()),
])
def test_place_names_in_a_title(title, names):
    assert place_names(title) == names


def test_at_most_three_names_are_tried():
    assert len(place_names("TC Alfa Beta Gama Delta, Mall Epsilon")) == 3


# ---------------------------------------------------------------- the map search
def hit(lat, lon):
    return httpx.Response(200, json=[{"lat": str(lat), "lon": str(lon), "display_name": "x"}])


@respx.mock
def test_a_named_place_inside_the_town_is_used_as_the_exact_spot(db):
    route = respx.get(NOMINATIM_URL).mock(return_value=hit(*PORTANOVA))
    res = Geocoder(db, sleep=lambda s: None).resolve("Osijek", None, ("Portanova",))
    assert res.precision == "address" and (res.lat, res.lon) == PORTANOVA
    assert route.calls[0].request.url.params["q"] == "Portanova, Osijek"  # the name and the town


@respx.mock
def test_a_hit_in_another_town_is_ignored_and_the_town_centre_stays(db):
    respx.get(NOMINATIM_URL).mock(return_value=hit(*ZAGREB))
    res = Geocoder(db, sleep=lambda s: None).resolve("Osijek", None, ("Portanova",))
    assert res.precision == "city" and (res.lat, res.lon) == OSIJEK


@respx.mock
def test_names_are_tried_in_order_and_the_first_good_hit_wins(db):
    route = respx.get(NOMINATIM_URL).mock(side_effect=[
        httpx.Response(200, json=[]), hit(*PORTANOVA)])
    names = ("Portanova Ulica", "Portanova")
    res = Geocoder(db, sleep=lambda s: None).resolve("Osijek", None, names)
    assert res.precision == "address" and route.call_count == 2


@respx.mock
def test_the_town_is_not_searched_twice_and_a_town_name_is_not_a_place(db):
    route = respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    g = Geocoder(db, sleep=lambda s: None)
    # "Portanova Osijek" and "Portanova" are the same search once the town is taken off
    assert g.resolve("Osijek", None, ("Portanova Osijek", "Portanova")).precision == "city"
    assert route.call_count == 1
    # a town or district name is no place: it would only find the middle of the town
    assert g.resolve("Osijek", None, ("Osijek", "Retfala", "Osijek Retfala")).precision == "city"
    assert route.call_count == 1


def test_without_names_or_a_town_there_is_no_search(db):
    with respx.mock(assert_all_called=False) as m:
        route = m.get(NOMINATIM_URL)
        g = Geocoder(db, sleep=lambda s: None)
        assert g.resolve("Osijek", None, ()).precision == "city"
        assert g.resolve(None, None, ("Portanova",)) is None
        assert route.call_count == 0


@respx.mock
def test_the_answer_is_saved_so_the_same_name_is_searched_once(db):
    route = respx.get(NOMINATIM_URL).mock(return_value=hit(*PORTANOVA))
    g = Geocoder(db, sleep=lambda s: None)
    g.resolve("Osijek", None, ("Portanova",))
    g.resolve("Osijek", None, ("Portanova",))
    assert route.call_count == 1


# ---------------------------------------------------------------- scoring
def test_a_town_only_ad_claims_no_minutes_for_a_place_inside_the_town(db):
    result = score_job(job(), profile(db), [home()], NOW)
    t = travel(result)
    assert not result.excluded
    assert t["data"] == {"unknown": True, "city_only": True, "place": "Dom"}
    # "probably near": 80 % of the 30 travel points (Ivan's choice, D40)
    assert "Dom" in t["text"] and t["pts"] == 24.0


def test_the_same_ad_at_an_exact_spot_gets_real_minutes(db):
    result = score_job(job(precision="address", at=PORTANOVA), profile(db), [home()], NOW)
    assert travel(result)["data"]["minutes"] is not None
    assert "city_only" not in travel(result)["data"]


def test_old_ads_without_a_precision_are_scored_as_before(db):
    result = score_job(job(precision=None), profile(db), [home()], NOW)
    assert travel(result)["data"]["minutes"] is not None


def test_a_town_only_ad_is_not_set_aside_for_being_too_far_from_a_place_in_its_town(db):
    # the town point is 3.5 km from this place, but the real job could be next door
    edge = home(lat=OSIJEK[0] + 0.03, max_minutes_by_mode={"walk": 20})
    result = score_job(job(), profile(db), [edge], NOW)
    assert not result.excluded and travel(result)["data"]["city_only"] is True


def test_a_town_only_ad_in_a_far_town_is_still_set_aside(db):
    result = score_job(job(at=ZAGREB, city="Zagreb"), profile(db), [home()], NOW)
    assert result.excluded and "Too far" in result.reasons[0]["text"]


def test_a_place_far_from_the_town_keeps_a_rough_estimate_marked_rough(db):
    partner = home(label="Partner", lat=DAKOVO[0], lon=DAKOVO[1],
                   max_minutes_by_mode={"car": 80})
    profile_with_car = profile(db)
    profile_with_car.has_car = True
    result = score_job(job(), profile_with_car, [home(), partner], NOW)
    data = travel(result)["data"]
    assert data["place"] == "Partner" and data["approx"] is True and data["minutes"] > 20


def test_a_town_only_ad_with_a_far_place_only_is_still_set_aside_when_too_far(db):
    partner = home(label="Partner", lat=DAKOVO[0], lon=DAKOVO[1], max_minutes_by_mode={"car": 20})
    p = profile(db)
    p.has_car = True
    assert score_job(job(), p, [partner], NOW).excluded


# ---------------------------------------------------------------- collecting a new ad
def raw(title=TITLE, address=None):
    return RawJob(external_id="e1", url="https://example.hr/oglas/1", title=title,
                  employer="Surfer d.o.o.", city="Osijek", address=address, pay_eur_hour=8.0,
                  description="Traži se djelatnik.", contact_text="posao@example.hr",
                  posted_at=NOW)


@pytest.fixture
def env(db):
    source = Source(key="t", name="studentski-poslovi.hr")
    db.add_all([source, home()])
    db.commit()
    prof = profile(db)
    prof.full_name = "Ivan Primjer"
    return db, prof, db.query(Place).all(), source


@respx.mock
def test_a_new_ad_with_a_known_centre_gets_its_real_spot_and_real_minutes(env):
    db, prof, places, source = env
    respx.get(NOMINATIM_URL).mock(return_value=hit(*PORTANOVA))
    saved = ingest_raw(db, source, raw(), prof, places, Geocoder(db, sleep=lambda s: None),
                       now=NOW)
    assert saved.geo_precision == "address" and (saved.lat, saved.lon) == PORTANOVA
    assert travel_data(saved)["minutes"] > 3


@respx.mock
def test_a_new_ad_whose_centre_is_not_found_says_the_time_is_unknown(env):
    db, prof, places, source = env
    respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    saved = ingest_raw(db, source, raw(), prof, places, Geocoder(db, sleep=lambda s: None),
                       now=NOW)
    assert saved.geo_precision == "city" and (saved.lat, saved.lon) == OSIJEK
    assert travel_data(saved)["city_only"] is True and saved.status != "ignored"


def test_an_ad_without_a_place_name_makes_no_map_search(env):
    db, prof, places, source = env
    with respx.mock(assert_all_called=False) as m:
        route = m.get(NOMINATIM_URL)
        saved = ingest_raw(db, source, raw(title="Konobar/ica vikendom"), prof, places,
                           Geocoder(db, sleep=lambda s: None), now=NOW)
        assert route.call_count == 0
    assert saved.geo_precision == "city"


def travel_data(saved: Job) -> dict:
    return next(r for r in saved.score_reasons if r["k"] == "travel")["data"]


# ---------------------------------------------------------------- old ads, checked once
def old_job(db, source, n, *, title="Konobar", at=OSIJEK, **kw) -> Job:
    values = dict(source=source, external_id=f"o{n}", url=f"https://example.hr/o/{n}",
                  title=title, city="Osijek", lat=at[0], lon=at[1], status="new", channel="email",
                  category="ugostiteljstvo", pay_eur_hour=8.0, score=90,
                  first_seen_at=NOW - timedelta(days=1), alerted_at=NOW - timedelta(hours=20),
                  score_reasons=[{"k": "travel", "pts": 28, "text": "3 min by walk from Dom",
                                  "data": {"minutes": 3, "mode": "walk", "place": "Dom"}}])
    values.update(kw)
    row = Job(**values)
    db.add(row)
    db.flush()
    return row


def geocoder(db):
    return Geocoder(db, sleep=lambda s: None)


@respx.mock
def test_an_old_town_only_ad_is_moved_to_its_real_spot_and_scored_again(env):
    db, prof, places, source = env
    respx.get(NOMINATIM_URL).mock(return_value=hit(*PORTANOVA))
    portanova = old_job(db, source, 1, title=TITLE)
    assert recheck_locations(db, geocoder(db), prof, places, now=NOW) == 1
    assert portanova.geo_precision == "address" and (portanova.lat, portanova.lon) == PORTANOVA
    data = travel_data(portanova)
    assert data["minutes"] > 3 and data["approx"] is True  # the rough estimate: no street map here
    assert portanova.status == "new"  # a move never changes what Ivan already saw


@respx.mock
def test_an_old_town_only_ad_without_a_findable_place_stops_claiming_minutes(env):
    db, prof, places, source = env
    respx.get(NOMINATIM_URL).mock(return_value=httpx.Response(200, json=[]))
    surfer = old_job(db, source, 1, title=TITLE)
    plain = old_job(db, source, 2, title="Konobar/ica")
    recheck_locations(db, geocoder(db), prof, places, now=NOW)
    for row in (surfer, plain):
        assert row.geo_precision == "city"
        assert travel_data(row) == {"unknown": True, "city_only": True, "place": "Dom"}


def test_an_old_ad_at_a_real_address_is_only_marked_and_keeps_its_minutes(env):
    db, prof, places, source = env
    with respx.mock(assert_all_called=False) as m:
        route = m.get(NOMINATIM_URL)
        exact = old_job(db, source, 1, title=TITLE, at=PORTANOVA)
        recheck_locations(db, geocoder(db), prof, places, now=NOW)
        assert route.call_count == 0  # no search for an ad that already has a real spot
    assert exact.geo_precision == "address" and travel_data(exact)["minutes"] > 3


def test_each_old_ad_is_checked_once_and_the_event_says_how_many(env):
    db, prof, places, source = env
    old_job(db, source, 1)
    old_job(db, source, 2, at=PORTANOVA)
    old_job(db, source, 3, lat=None, lon=None)  # no location at all: nothing to check
    assert recheck_locations(db, geocoder(db), prof, places, now=NOW) == 2
    assert recheck_locations(db, geocoder(db), prof, places, now=NOW) == 0
    [event] = db.query(Event).filter_by(kind="locations_rechecked").all()
    assert event.data["checked"] == 2 and event.data["found"] == 0
    assert "Provjerena je lokacija za 2 oglasa" in load_log(db).entries[0].text


def test_a_batch_takes_only_so_many_ads(env):
    db, prof, places, source = env
    for n in range(5):
        old_job(db, source, n)
    assert recheck_locations(db, geocoder(db), prof, places, now=NOW, limit=3) == 3
    assert recheck_locations(db, geocoder(db), prof, places, now=NOW, limit=3) == 2


def test_an_ad_set_aside_by_the_old_wrong_minutes_comes_back_without_an_alert(env):
    db, prof, places, source = env
    # an edge place 3.5 km from the town point: the old rough minutes said "too far"
    db.query(Place).delete()
    db.add(home(lat=OSIJEK[0] + 0.03, max_minutes_by_mode={"walk": 20}))
    db.flush()
    places = db.query(Place).all()
    aside = old_job(db, source, 1, status="ignored", score=0, alerted_at=None,
                    score_reasons=[{"k": "travel", "pts": 0, "text": "Too far", "data": {}}])
    recheck_locations(db, geocoder(db), prof, places, now=NOW)
    assert aside.status == "new" and aside.score > 0
    assert aside.alerted_at is not None  # back among the open ads, but no email and no push


def test_an_ad_older_than_a_month_is_marked_but_not_scored_again(env):
    db, prof, places, source = env
    ancient = old_job(db, source, 1, first_seen_at=NOW - timedelta(days=45))
    recheck_locations(db, geocoder(db), prof, places, now=NOW)
    assert ancient.geo_precision == "city" and ancient.score == 90


def test_the_locator_the_scheduler_calls_uses_its_own_session(factory):  # noqa: F811
    with factory() as s:
        src = Source(key="t", name="t")
        s.add_all([src, home()])
        s.flush()
        old_job(s, src, 1, first_seen_at=datetime.now(UTC))
        s.commit()

    def offline(session):
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        return Geocoder(session, http=http, sleep=lambda s: None)

    assert make_locator(factory, offline)() == 1
    with factory() as s:
        assert s.query(Job).one().geo_precision == "city"


def test_the_locator_does_nothing_and_opens_no_map_connection_when_nothing_is_left(factory):  # noqa: F811
    def never(session):
        raise AssertionError("no map connection may be made when there is nothing to check")

    assert make_locator(factory, never)() == 0


# ---------------------------------------------------------------- the scheduler
class Recorder:
    def __init__(self, *, fail=None, gate=None):
        self.calls, self.fail, self.gate = 0, fail, gate
        self.entered = threading.Event()

    def __call__(self):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"
        if self.fail:
            raise self.fail


def test_without_a_locator_nothing_is_scheduled(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"))
    assert sched.locate_tick() == "none"
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a"]
    finally:
        sched.shutdown()


def test_with_a_locator_a_job_runs_every_five_minutes(factory):  # noqa: F811
    sched = make(factory, FakeSource("a"), locator=Recorder())
    sched.start()
    try:
        assert sched.scheduled_keys() == ["a", "locate"]
        assert sched._scheduler.get_job("locate").trigger.interval == timedelta(minutes=5)
    finally:
        sched.shutdown()


def test_a_failing_locator_never_raises_and_never_overlaps(factory):  # noqa: F811
    failing = Recorder(fail=RuntimeError("map search exploded"))
    sched = make(factory, FakeSource("a"), locator=failing)
    assert sched.locate_tick() == "error"
    assert sched.run_source("a") == "ran"  # polling is not affected

    gate = threading.Event()
    slow = Recorder(gate=gate)
    sched = make(factory, FakeSource("a"), locator=slow)
    worker = threading.Thread(target=sched.locate_tick)
    worker.start()
    assert slow.entered.wait(10)
    try:
        assert sched.locate_tick() == "busy"
    finally:
        gate.set()
        worker.join(10)
    assert slow.calls == 1


# ---------------------------------------------------------------- what Ivan reads
CITY_ONLY = [{"k": "travel", "pts": 12, "text": "x",
              "data": {"unknown": True, "city_only": True, "place": "Dom"}}]


def test_the_alert_says_only_the_town_is_known():
    [line] = reasons_hr(CITY_ONLY)
    assert "oglas navodi samo grad" in line and "min" not in line
    plain = reasons_hr([{"k": "travel", "pts": 12, "text": "x", "data": {"unknown": True}}])
    assert "oglas navodi samo grad" not in plain[0]


def test_the_row_knows_the_time_is_unknown_because_only_the_town_is_named(db):
    source = Source(key="t", name="Test izvor")
    db.add(source)
    db.flush()
    row = build_row(old_job(db, source, 1, score_reasons=CITY_ONLY), get_or_create_profile(db), NOW)
    assert row.travel_city_only and row.travel_unknown and row.travel_minutes is None


@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    return TestClient(app, follow_redirects=False)


def test_the_inbox_and_the_job_page_say_it_in_plain_words(client, db):
    source = Source(key="t", name="studentski-poslovi.hr")
    db.add(source)
    db.flush()
    portanova = old_job(db, source, 1, title=TITLE, status="seen", score_reasons=CITY_ONLY)
    inbox = client.get("/poslovi").text
    assert "Put nije poznat: oglas navodi samo grad" in inbox
    assert " min</strong>" not in inbox
    page = client.get(f"/poslovi/{portanova.id}").text
    assert "nije poznat: oglas navodi samo grad (Osijek)" in page
    assert "Točnu adresu potražite u oglasu" in page


def test_the_migration_keeps_every_job_and_leaves_the_new_column_empty(tmp_path):
    from alembic import command
    from sqlalchemy import text

    from app.core.db import make_engine
    from tests.test_migrations_keep_data import cfg_for

    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = cfg_for(url)
    command.upgrade(cfg, "f7b2c4d8e9a1")
    engine = make_engine(url)
    with engine.begin() as c:
        c.execute(text("INSERT INTO source (id, key, name, enabled, interval_minutes,"
                       " consecutive_failures, last_items) VALUES (1, 's', 's', 1, 10, 0, 0)"))
        c.execute(text(
            "INSERT INTO job (id, source_id, external_id, url, title, status, channel, lat, lon,"
            " first_seen_at) VALUES (1, 1, 'e1', 'https://x', 'TC Portanova', 'new', 'email',"
            " 45.555, 18.6955, '2026-10-09 00:00:00')"))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(url)
    with engine.connect() as c:
        row = tuple(c.execute(text("SELECT title, lat, lon, geo_precision FROM job")).one())
        fk_errors = c.execute(text("PRAGMA foreign_key_check")).fetchall()
    engine.dispose()
    assert row == ("TC Portanova", 45.555, 18.6955, None) and fk_errors == []

