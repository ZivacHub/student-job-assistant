"""Step 4.9 (G4): the one-time street map download and the map files on this PC.

No test touches the internet: every request goes to a stand-in server (httpx MockTransport).
"""

import json
import re
from urllib.parse import parse_qs

import httpx
import pytest

from app.geo import mapstore, osm_download
from app.geo.mapstore import AREAS, Area
from app.geo.osm_download import MapDownloadError, build_queries, download_all, download_area
from app.geo.streets import StreetMap
from tests.street_fixtures import bus_route, doc, pt, signal, town, tram_route

DETAILED = AREAS[0]
MAIN_ROADS = AREAS[1]
TINY = Area("test", "Test: mala karta", (45.50, 18.60, 45.60, 18.80), "detailed")


class FakeServer:
    """Answers like Overpass: street ways, signal nodes, tram or bus routes, by the query."""

    def __init__(self, failures=(), ways=None):
        self.failures = list(failures)  # status codes (or callables) handed out first
        self.requests: list[httpx.Request] = []
        self.ways = ways if ways is not None else town()["elements"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            return failure(request) if callable(failure) else httpx.Response(failure, text="busy")
        query = parse_qs(request.content.decode())["data"][0]
        if '"route"="bus"' in query:
            elements = bus_route(8, "1", "1: Zapad => Istok", [("Zapad", 0, 3), ("Sredina", 2, 3),
                                                             ("Istok", 3, 3)],
                                 origin="Zapad", destination="Istok")
        elif "relation" in query:
            elements = [tram_route(7, "1", [(0, 3), (3, 3)])]
        elif "traffic_signals" in query:
            elements = [signal(2, 1)]
        else:
            elements = self.ways
        return httpx.Response(200, json={"elements": elements})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def queries(self) -> list[str]:
        return [parse_qs(r.content.decode())["data"][0] for r in self.requests]


@pytest.fixture(autouse=True)
def small_maps(monkeypatch):
    """The stand-in town has 8 streets; the real minimum (hundreds) is tested separately."""
    monkeypatch.setitem(osm_download.MIN_WAYS, "detailed", 5)
    monkeypatch.setitem(osm_download.MIN_WAYS, "main_roads", 5)
    mapstore.forget_street_map()
    yield
    mapstore.forget_street_map()


def sleeps():
    waited: list[float] = []
    return waited, waited.append


# ---------------------------------------------------------------- what is asked
def test_a_detailed_area_asks_for_streets_lights_tram_and_buses():
    labels = [what for what, _query in build_queries(DETAILED)]
    assert labels == ["ulice i staze", "semafori i pješački prijelazi", "linije tramvaja",
                      "linije autobusa"]


def test_the_bus_question_asks_for_stops_in_order_not_for_every_street_piece():
    """Bus routes are asked as relations with their members, plus the stop nodes they name."""
    query = dict(build_queries(DETAILED))["linije autobusa"]
    assert '"route"="bus"' in query and "out body" in query and "node(r.r)" in query
    assert "out geom" not in query  # the streets of a route are not downloaded again


def test_the_main_roads_area_asks_for_main_roads_only():
    [(_what, query)] = build_queries(MAIN_ROADS)
    assert "motorway|trunk|primary|secondary|tertiary" in query and "residential" not in query


def test_a_request_carries_only_the_map_area_never_a_place_of_ivans(tmp_path):
    server = FakeServer()
    download_area(TINY, tmp_path, client=server.client(), sleep=lambda _s: None)
    allowed = {"45.5", "18.6", "45.6", "18.8"}  # the box of the area and nothing else
    assert len(server.requests) == 4
    for request in server.requests:
        assert list(parse_qs(request.content.decode())) == ["data"]  # one field: the question
    for query in server.queries():
        numbers = set(re.findall(r"\d+\.\d+", query))
        assert numbers <= allowed, numbers
        assert "45.5,18.6,45.6,18.8" in query


# ---------------------------------------------------------------- what is saved
def test_the_answers_are_saved_as_one_map_file_and_it_builds_a_street_map(tmp_path):
    server = FakeServer()
    size = download_area(TINY, tmp_path, client=server.client(), sleep=lambda _s: None)
    path = mapstore.area_file(TINY, tmp_path)
    assert size == path.stat().st_size > 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    kinds = [e["type"] for e in saved["elements"]]
    assert kinds.count("way") == 8 and kinds.count("relation") == 2  # the tram and the bus route
    assert kinds.count("node") == 1 + 3  # the light and the three bus stops
    sm = StreetMap.from_osm([saved])
    assert sm.minutes("walk", pt(2, 3), pt(8, 3)) == pytest.approx(15.0, abs=0.1)
    drive_s = 1200 / (30 / 3.6) + 300 + 20  # street time + parking + the light on the way
    assert sm.minutes("car", pt(2, 3), pt(8, 3)) == pytest.approx(drive_s / 60, abs=0.1)
    meta = json.loads(mapstore.meta_file(TINY, tmp_path).read_text(encoding="utf-8"))
    assert meta["ways"] == 8 and "OpenStreetMap" in meta["source"]
    assert meta["bus_routes"] == 1
    assert sm.bus is not None and sm.bus.lines == ["1"]
    assert not list(tmp_path.glob("maps/*.part"))


def test_progress_is_reported_in_croatian(tmp_path):
    seen: list[str] = []
    download_area(TINY, tmp_path, client=FakeServer().client(), sleep=lambda _s: None,
                  progress=seen.append)
    assert len(seen) == 4 and "ulice i staze (1/4)" in seen[0]
    assert "linije autobusa (4/4)" in seen[3]


# ---------------------------------------------------------------- a busy server
def test_a_busy_server_is_retried_on_another_server_after_a_pause(tmp_path):
    server = FakeServer(failures=[502])
    waited, sleep = sleeps()
    download_area(TINY, tmp_path, client=server.client(), sleep=sleep)
    assert waited == [osm_download.PAUSES_S[0]]
    first, second = server.requests[0].url, server.requests[1].url
    assert str(first) == osm_download.SERVERS[0] and str(second) == osm_download.SERVERS[1]


def test_a_broken_or_unfinished_answer_is_retried(tmp_path):
    server = FakeServer(failures=[
        lambda _r: httpx.Response(200, text="<html>not json</html>"),
        lambda _r: httpx.Response(200, json={"elements": [], "remark": "runtime error: timed out"}),
        lambda _r: httpx.Response(200, json={"nothing": 1}),
    ])
    waited, sleep = sleeps()
    download_area(TINY, tmp_path, client=server.client(), sleep=sleep)
    assert len(waited) == 3 and mapstore.area_file(TINY, tmp_path).is_file()


def test_a_server_that_never_answers_gives_a_plain_message_and_no_file(tmp_path):
    server = FakeServer(failures=[503] * 20)
    waited, sleep = sleeps()
    with pytest.raises(MapDownloadError, match="Pokušajte ponovno za nekoliko minuta"):
        download_area(TINY, tmp_path, client=server.client(), sleep=sleep)
    assert waited == list(osm_download.PAUSES_S)  # every pause was used, then it gave up
    assert not mapstore.area_file(TINY, tmp_path).exists()


def test_a_lost_connection_is_retried_too(tmp_path):
    def drop(request):
        raise httpx.ConnectError("no network", request=request)

    server = FakeServer(failures=[drop])
    download_area(TINY, tmp_path, client=server.client(), sleep=lambda _s: None)
    assert mapstore.area_file(TINY, tmp_path).is_file()


def test_a_refusal_is_not_retried(tmp_path):
    server = FakeServer(failures=[403])
    with pytest.raises(MapDownloadError, match="403"):
        download_area(TINY, tmp_path, client=server.client(), sleep=lambda _s: None)
    assert len(server.requests) == 1


# ---------------------------------------------------------------- a bad download never wins
def test_an_empty_answer_is_not_saved_as_a_map(tmp_path, monkeypatch):
    monkeypatch.setitem(osm_download.MIN_WAYS, "detailed", 500)  # the real minimum
    server = FakeServer()  # the stand-in town has only 8 streets
    with pytest.raises(MapDownloadError, match="prazna ili nepotpuna"):
        download_area(TINY, tmp_path, client=server.client(), sleep=lambda _s: None)
    assert not mapstore.area_file(TINY, tmp_path).exists()


def test_the_old_map_stays_when_a_new_download_fails(tmp_path):
    path = mapstore.area_file(TINY, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"elements": []}', encoding="utf-8")
    server = FakeServer(failures=[502] * 20)
    with pytest.raises(MapDownloadError):
        download_area(TINY, tmp_path, client=server.client(), sleep=lambda _s: None)
    assert path.read_text(encoding="utf-8") == '{"elements": []}'


def test_download_all_fetches_every_area_and_drops_the_map_from_memory(tmp_path):
    server = FakeServer()
    mapstore.get_street_map(tmp_path)  # remembers "no map"
    total = download_all(tmp_path, client=server.client(), sleep=lambda _s: None)
    assert total > 0 and len(server.requests) == 4 + 1  # four for the city, one for the region
    assert all(s.present for s in mapstore.status(tmp_path))
    assert mapstore.get_street_map(tmp_path) is not None  # forgotten, so it is built again


# ---------------------------------------------------------------- the files on this PC
def test_status_says_what_is_there(tmp_path):
    assert [s.present for s in mapstore.status(tmp_path)] == [False, False]
    download_area(DETAILED, tmp_path, client=FakeServer().client(), sleep=lambda _s: None)
    first, second = mapstore.status(tmp_path)
    assert first.present and first.size_bytes > 0 and first.downloaded_at is not None
    assert not second.present and second.size_bytes == 0


def test_status_tells_a_map_from_before_the_bus_lines_from_one_with_them(tmp_path):
    download_area(DETAILED, tmp_path, client=FakeServer().client(), sleep=lambda _s: None)
    assert mapstore.status(tmp_path)[0].bus_routes == 1
    meta_path = mapstore.meta_file(DETAILED, tmp_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["bus_routes"]  # a map downloaded before step 4.12
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert mapstore.status(tmp_path)[0].bus_routes is None
    meta_path.write_text("broken", encoding="utf-8")  # an unreadable note: still no claim
    assert mapstore.status(tmp_path)[0].bus_routes is None


def test_no_files_means_no_street_map(tmp_path):
    assert mapstore.get_street_map(tmp_path) is None


def test_the_map_is_built_once_and_again_when_a_file_changes(tmp_path):
    download_area(DETAILED, tmp_path, client=FakeServer().client(), sleep=lambda _s: None)
    first = mapstore.get_street_map(tmp_path)
    assert first is not None and mapstore.get_street_map(tmp_path) is first
    # a second download changes the file, so the map is rebuilt from it
    download_area(DETAILED, tmp_path, client=FakeServer(ways=doc(*town()["elements"][:5])
                                                        ["elements"]).client(),
                  sleep=lambda _s: None)
    second = mapstore.get_street_map(tmp_path)
    assert second is not first and second.way_count == 5


def test_a_file_that_cannot_be_read_is_skipped_not_fatal(tmp_path):
    path = mapstore.area_file(DETAILED, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("this is not json", encoding="utf-8")
    assert mapstore.get_street_map(tmp_path) is None
