"""Step 4.9 (G6): the Mjesta page and the street map: Tramvaj row, download button, reach area."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core import map_download
from app.core.db import get_session
from app.core.event_log import load_log
from app.core.map_areas import MAX_RECTANGLES, reach_rectangles
from app.core.profile import get_or_create_profile
from app.geo import mapstore, osm_download
from app.geo.streets import StreetMap
from app.main import create_app
from app.models import Event, Place
from app.web import map_routes
from tests.street_fixtures import doc, line, pt, town, way
from tests.test_osm_download import FakeServer
from tests.test_web_places import ORIGIN, place_post

HOME = pt(2, 3)


@pytest.fixture
def maps_dir(tmp_path, monkeypatch):
    """The map files live in a throwaway folder, never in the real data folder."""
    monkeypatch.setattr(mapstore, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    mapstore.forget_street_map()
    map_download.reset_state()
    yield tmp_path
    mapstore.forget_street_map()
    map_download.reset_state()


def put_map(folder, elements=None):
    path = mapstore.area_file(mapstore.AREAS[0], folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"elements": elements or town()["elements"]}), encoding="utf-8")
    mapstore.forget_street_map()


@pytest.fixture
def client(db, maps_dir):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    return TestClient(app, follow_redirects=False)


def add_place(db, minutes=None, **kw):
    values = dict(label="Dom", kind="home", lat=HOME[0], lon=HOME[1],
                  max_minutes_by_mode=minutes or {"walk": 6}, active=True)
    values.update(kw)
    place = Place(**values)
    db.add(place)
    db.flush()
    return place


# ---------------------------------------------------------------- the Tramvaj row
def test_every_place_form_has_a_tram_row_next_to_the_other_ways(client):
    html = client.get("/mjesta").text
    assert 'name="min_tram"' in html and ">Tramvajem</label>" in html
    assert "Tramvaj je procjena" in html and "samo ako tramvaj doista vozi" in html


def test_tram_minutes_are_saved_and_shown_in_the_summary(client, db):
    r = client.post("/mjesta/mjesto", data=place_post(min_tram="25", min_bus=""), headers=ORIGIN)
    assert r.status_code == 303
    place = db.query(Place).one()
    assert place.max_minutes_by_mode == {"tram": 25}
    assert "tramvajem 25 min" in client.get("/mjesta").text


def test_tram_minutes_are_checked_like_the_others(client):
    r = client.post("/mjesta/mjesto", data=place_post(min_tram="999"), headers=ORIGIN)
    assert r.status_code == 200 and "Minute moraju biti između 1 i 240" in r.text
    assert 'href="#min-tram-new"' in r.text


def test_the_buttons_that_repeat_for_every_place_name_their_place(client, db):
    add_place(db, label="Dom")
    add_place(db, label="Fakultet", lat=pt(8, 3)[0], lon=pt(8, 3)[1])
    html = client.get("/mjesta").text
    for name in ("Dom", "Fakultet"):
        for visible in ("Spremi mjesto", "Odustani", "Obriši mjesto"):
            hidden = f'{visible}<span class="visually-hidden">: {name}</span>'
            assert hidden in html, (visible, name)
    assert 'Spremi mjesto</button>' in html  # the form for a new place has no name to add


# ---------------------------------------------------------------- the street map section
def test_without_a_map_the_page_offers_the_download_and_says_why(client, db):
    add_place(db)
    html = client.get("/mjesta").text
    assert "Preuzmite kartu ulica" in html
    assert '<form method="post" action="/mjesta/karta/preuzmi">' in html
    assert "oko 13 MB" in html and "nikakvo Vaše mjesto ni adresa" in html
    assert 'data-streets="false"' in html and 'id="map-modes"' not in html
    assert "Krug je gruba procjena" in html
    assert "OpenStreetMapa (licenca ODbL)" in html


def test_with_a_map_the_page_says_so_and_offers_the_area_per_way_of_travelling(client, db,
                                                                              maps_dir):
    add_place(db, {"walk": 20, "tram": 15, "bus": 30})
    put_map(maps_dir)
    html = client.get("/mjesta").text
    assert "Karta je preuzeta" in html and "Preuzmi kartu ponovno" in html
    assert 'data-streets="true"' in html
    assert 'id="map-modes"' in html
    assert 'value="walk" checked' in html and 'value="tram"' in html
    assert 'value="bus"' not in html and 'value="car"' not in html  # no map data / no car
    assert "Obojeno je područje" in html
    assert "Preuzmite kartu ulica" not in html


def test_the_car_is_offered_only_with_a_car(client, db, maps_dir):
    add_place(db, {"car": 30})
    put_map(maps_dir)
    assert 'value="car"' not in client.get("/mjesta").text
    get_or_create_profile(db).has_car = True
    db.flush()
    assert 'value="car"' in client.get("/mjesta").text


def test_the_page_says_when_the_wider_map_is_missing(client, db, maps_dir):
    add_place(db)
    put_map(maps_dir)
    assert "Širu kartu glavnih cesta" in client.get("/mjesta").text


def test_a_map_from_before_the_bus_lines_says_to_download_it_again(client, db, maps_dir):
    add_place(db)
    put_map(maps_dir)  # a map file with no note of bus routes: downloaded before step 4.12
    html = client.get("/mjesta").text
    assert "data-needs-buses" in html and "prije nego što je dobila autobusne linije" in html
    assert "Preuzmi kartu ponovno" in html  # the button the hint points to


def test_a_map_that_has_the_bus_lines_does_not_ask_for_another_download(client, db, maps_dir,
                                                                       monkeypatch):
    add_place(db)
    monkeypatch.setitem(osm_download.MIN_WAYS, "detailed", 5)
    osm_download.download_area(mapstore.AREAS[0], maps_dir, client=FakeServer().client(),
                               sleep=lambda _s: None)
    mapstore.forget_street_map()
    html = client.get("/mjesta").text
    assert "Karta je preuzeta" in html and "data-needs-buses" not in html


def test_without_a_map_there_is_no_bus_hint(client, db):
    add_place(db)
    assert "data-needs-buses" not in client.get("/mjesta").text


# ---------------------------------------------------------------- the download button
def test_the_button_starts_the_download_in_the_background(client, monkeypatch):
    calls = []
    monkeypatch.setattr(map_routes.map_download, "start_download",
                        lambda factory, **kw: calls.append(factory) or True)
    r = client.post("/mjesta/karta/preuzmi", headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"] == "/mjesta?ok=karta-pokrenuta#karta"
    assert len(calls) == 1
    assert "Preuzimanje karte ulica je počelo." in client.get(r.headers["location"]).text


def test_a_second_press_while_it_runs_says_so(client, monkeypatch):
    monkeypatch.setattr(map_routes.map_download, "start_download", lambda factory, **kw: False)
    r = client.post("/mjesta/karta/preuzmi", headers=ORIGIN)
    assert r.headers["location"] == "/mjesta?ok=karta-vec#karta"
    assert "već traje" in client.get(r.headers["location"]).text


def test_the_button_only_works_from_this_pages_own_address(client):
    assert client.post("/mjesta/karta/preuzmi").status_code == 403  # no Origin header
    assert client.post("/mjesta/karta/preuzmi", headers={"origin": "http://evil.example"}
                       ).status_code == 403


def test_while_it_runs_the_page_shows_progress_reloads_and_can_be_stopped(client, db):
    add_place(db)
    map_download._set(running=True, step="ulice i staze (1/3)")
    html = client.get("/mjesta").text
    assert "Preuzimanje karte je u tijeku: ulice i staze (1/3)." in html
    assert '<meta http-equiv="refresh" content="5">' in html
    assert "Zaustavi automatsko osvježavanje" in html
    stopped = client.get("/mjesta?auto=0").text
    assert 'http-equiv="refresh"' not in stopped and "Osvježi stranicu" in stopped


def test_a_failed_download_shows_the_plain_message(client):
    map_download._set(error="Karta se nije mogla preuzeti. Pokušajte ponovno za nekoliko minuta.")
    html = client.get("/mjesta").text
    assert 'role="alert" data-error-summary' in html and "nije mogla preuzeti" in html


# ---------------------------------------------------------------- the whole download
def stand_in_world(monkeypatch):
    monkeypatch.setitem(osm_download.MIN_WAYS, "detailed", 5)
    monkeypatch.setitem(osm_download.MIN_WAYS, "main_roads", 5)


def test_a_download_scores_every_ad_again_and_logs_it(db, maps_dir, monkeypatch):
    stand_in_world(monkeypatch)
    add_place(db, {"walk": 12})
    map_download.run_download(lambda: nullcontext(db), maps_dir, client=FakeServer().client(),
                              sleep=lambda _s: None)
    state = map_download.snapshot()
    assert not state.running and state.error is None and state.size_bytes > 0
    assert state.rescored == 0 and state.finished_at is not None  # no ads in this database
    assert all(f.present for f in mapstore.status(maps_dir))
    [event] = db.query(Event).filter_by(kind="map_downloaded").all()
    assert event.data["bytes"] == state.size_bytes
    text = load_log(db).entries[0].text
    assert text.startswith("Karta ulica je preuzeta (") and "ponovno izračunata" in text


def test_a_failed_download_is_remembered_and_logged_in_plain_words(db, maps_dir, monkeypatch):
    stand_in_world(monkeypatch)
    server = FakeServer(failures=[503] * 30)
    map_download.run_download(lambda: nullcontext(db), maps_dir, client=server.client(),
                              sleep=lambda _s: None)
    state = map_download.snapshot()
    assert not state.running and "Pokušajte ponovno" in state.error
    assert not any(f.present for f in mapstore.status(maps_dir))
    entry = load_log(db).entries[0]
    assert entry.text.startswith("Preuzimanje karte ulica nije uspjelo.") and entry.level == "warn"


def test_an_unexpected_error_gives_a_plain_message_not_a_stack_trace(db, maps_dir, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(map_download, "download_all", boom)
    map_download.run_download(lambda: nullcontext(db), maps_dir)
    state = map_download.snapshot()
    assert "neočekivane greške" in state.error and "secret detail" not in state.error


def test_only_one_download_runs_at_a_time(monkeypatch):
    started = []
    monkeypatch.setattr(map_download, "run_download", lambda *a, **kw: started.append(1))
    map_download.reset_state()
    assert map_download.start_download(lambda: nullcontext(None)) is True
    assert map_download.start_download(lambda: nullcontext(None)) is False
    map_download.reset_state()


# ---------------------------------------------------------------- the area on the map
def test_the_area_is_served_per_way_of_travelling(client, db, maps_dir):
    add_place(db, {"walk": 6})
    put_map(maps_dir)
    data = client.get("/mjesta/podrucje?nacin=walk").json()
    assert data["street_map"] is True and data["mode"] == "walk"
    [place] = data["places"]
    assert place["label"] == "Dom" and place["minutes"] == 6 and place["rects"]
    south, west, north, east = place["rects"][0]
    assert south < north and west < east


def test_the_area_follows_the_streets_and_stops_at_the_river(maps_dir):
    sm = StreetMap.from_osm([town()])
    rects = reach_rectangles(sm, "walk", HOME, 6)  # 6 minutes = 480 m

    def covered(i, j):
        lat, lon = pt(i, j)
        return any(s <= lat <= n and w <= lon <= e for s, w, n, e in rects)

    assert covered(3, 0)  # 400 m by the streets
    assert covered(2, 3)  # the place itself
    assert not covered(8, 3)  # 600 m in a straight line, but across the river
    assert not covered(6, 1)


def test_a_straight_street_is_one_strip_not_many_squares():
    sm = StreetMap.from_osm([doc(way(1, line(0, 0, 10, 0)))])
    rects = reach_rectangles(sm, "walk", pt(0, 0), 30)
    assert len(rects) == 1


def test_squares_grow_when_there_would_be_too_many(monkeypatch):
    rows = [way(i + 1, line(0, i, 30, i)) for i in range(0, 30)]
    columns = [way(i + 101, line(i, 0, i, 30)) for i in range(0, 30)]
    sm = StreetMap.from_osm([doc(*rows, *columns)])  # a 3 km x 3 km grid of streets
    many = reach_rectangles(sm, "walk", pt(0, 0), 60)
    monkeypatch.setattr("app.core.map_areas.MAX_RECTANGLES", 4)
    few = reach_rectangles(sm, "walk", pt(0, 0), 60)
    assert len(many) > 4 and len(few) <= 4 and MAX_RECTANGLES == 1500


def test_no_area_without_a_map_or_for_a_way_without_map_data(client, db, maps_dir):
    add_place(db, {"walk": 6, "bus": 20})
    assert client.get("/mjesta/podrucje?nacin=walk").json() == {
        "mode": "walk", "street_map": False, "places": []}
    put_map(maps_dir)
    assert client.get("/mjesta/podrucje?nacin=bus").json()["places"] == []
    assert client.get("/mjesta/podrucje?nacin=<script>").json()["places"] == []


def test_no_area_for_a_place_that_is_off_or_does_not_use_the_way(client, db, maps_dir):
    add_place(db, {"walk": 6}, active=False)
    add_place(db, {"bike": 10}, label="Fakultet")
    put_map(maps_dir)
    assert client.get("/mjesta/podrucje?nacin=walk").json()["places"] == []
    assert [p["label"] for p in client.get("/mjesta/podrucje?nacin=bike").json()["places"]] == [
        "Fakultet"]


def test_the_car_has_no_area_without_a_car(client, db, maps_dir):
    add_place(db, {"car": 20})
    put_map(maps_dir)
    assert client.get("/mjesta/podrucje?nacin=car").json()["places"] == []
    get_or_create_profile(db).has_car = True
    db.flush()
    assert len(client.get("/mjesta/podrucje?nacin=car").json()["places"]) == 1


# ---------------------------------------------------------------- the Inbox hint
def test_the_inbox_asks_for_the_map_only_when_there_are_places_and_no_map(client, db, maps_dir):
    assert "preuzmite kartu ulica" not in client.get("/poslovi").text  # no places yet
    add_place(db)
    note = client.get("/poslovi").text
    assert "gruba procjena po zračnoj liniji" in note and 'href="/mjesta#karta"' in note
    put_map(maps_dir)
    assert "gruba procjena po zračnoj liniji" not in client.get("/poslovi").text
