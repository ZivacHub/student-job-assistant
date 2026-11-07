"""Step 4.12 (D41): joining GPP's bus boards to the map's bus routes by place names, and saying in
Dnevnik what did not fit.

Hand-made routes for the rules; the real routes of the map server and the real 38 board names of
GPP's page (tests/fixtures/osm_bus/, both read on 2026-11-04) for the whole picture.
"""

import json
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core import map_download
from app.core.event_log import _describe, load_log
from app.geo import mapstore, osm_download
from app.geo.bus_match import (
    KIND,
    WHY_NO_ROUTE,
    WHY_ONE_PLACE,
    WHY_SEVERAL_LINES,
    WHY_TWO_LEGS,
    _names,
    board_places,
    match_boards,
    report_bus_lines,
)
from app.geo.cities import normalize
from app.geo.streets import StreetMap
from app.models import Event, TimetableBoard
from tests.street_fixtures import bus_route, doc, town
from tests.test_osm_download import FakeServer

FIXTURES = Path(__file__).parent / "fixtures" / "osm_bus"


def streets_with(*elements):
    return StreetMap.from_osm([doc(*town()["elements"], *elements)])


def line_six():
    return bus_route(31, "6", "6: Ivanovac => Osijek (Gajev trg) => Tvrđavica",
                     [("Ivanovac 1 / Duga ul.", 0, 3), ("Školska ul.", 1, 3),
                      ("Trg Lj. Gaja", 2, 3), ("Kopika", 3, 3), ("Tvrđavica 1", 4, 3)],
                     origin="Ivanovac", destination="Tvrđavica")


def match(elements, *texts):
    network = streets_with(*elements).bus
    return match_boards(network, list(enumerate(texts, 1)))


# ---------------------------------------------------------------- how a place is written
def test_a_place_in_brackets_is_also_known_by_what_is_in_the_bracket():
    names = _names("OSIJEK (GAJEV TRG)")
    assert {"osijek gajev trg", "gajev trg"} <= names
    assert "osijek" not in names  # the town alone would fit every stop in it


def test_the_main_square_is_the_same_place_under_both_of_its_names():
    assert _names("GAJEV TRG") & _names("Trg Lj. Gaja")


def test_a_slash_lists_two_places_and_a_platform_number_is_dropped_only_for_stops():
    assert {"sjp orao", "opus arena"} <= _names("SJP ORAO / OPUS ARENA")
    assert "ivanovac" in _names("Ivanovac 1 / Duga ul.", platform_number=True)
    assert "ivanovac" not in _names("Ivanovac 1 / Duga ul.")


def test_a_number_that_belongs_to_the_name_stays():
    assert _names("TENJA 4-3") == {"tenja 4 3"}


@pytest.mark.parametrize("text, count, why", [
    ("IVANOVAC - OSIJEK (GAJEV TRG)", 2, None),
    ("JUG II - KBC- JUG II", 3, None),  # a dash with a space on one side only
    ("TENJA 4-3 - OSIJEK (GAJEV TRG)", 2, None),  # a dash inside a name is not a separator
    ("GAJEV TRG -IND.ZONA NEMETIN, IND.ZONA NEMETIN-GAJEV TRG", 0, WHY_TWO_LEGS),
    ("RETFALA", 0, WHY_ONE_PLACE),
    ("", 0, WHY_ONE_PLACE),
])
def test_a_board_text_is_cut_into_its_places(text, count, why):
    places, reason = board_places(text)
    assert len(places) == count and reason == why


# ---------------------------------------------------------------- matching hand-made routes
def test_a_board_belongs_to_the_route_that_runs_from_its_first_place_to_its_last():
    result = match(line_six(), "IVANOVAC - OSIJEK (GAJEV TRG)", "GAJEV TRG - TVRĐAVICA")
    assert [(m.route, m.line, m.relations) for m in result.matched] == [
        ("IVANOVAC - OSIJEK (GAJEV TRG)", "6", (31,)), ("GAJEV TRG - TVRĐAVICA", "6", (31,))]
    assert result.unmatched == [] and result.lines == ["6"] and result.boards == 2


def test_the_other_direction_is_not_the_same_board():
    result = match(line_six(), "OSIJEK (GAJEV TRG) - IVANOVAC", "TVRĐAVICA - GAJEV TRG")
    assert result.matched == []
    assert [(u.why, u.lines) for u in result.unmatched] == [(WHY_NO_ROUTE, ())] * 2


def test_the_routes_own_name_and_tags_say_where_its_first_and_last_stop_are():
    """Its first stop is called after a street, but the route says it starts in Ivanovac."""
    elements = bus_route(32, "6", "6: Ivanovac => Osijek (Gajev trg) => Tvrđavica",
                         [("Okretište Čepinska ul.", 0, 3), ("Trg Lj. Gaja", 2, 3),
                          ("Okretište - ZOO", 4, 3)], destination="Tvrđavica")
    result = match(elements, "IVANOVAC - GAJEV TRG", "GAJEV TRG - TVRĐAVICA")
    assert [m.line for m in result.matched] == ["6", "6"]


def test_every_variant_that_fits_is_kept_and_a_variant_that_does_not_is_not():
    elements = [*line_six(),
                *bus_route(33, "6", "6: Ivanovac => Osijek (Gajev trg) => Kampus",
                           [("Ivanovac 1", 0, 4), ("Trg Lj. Gaja", 2, 4), ("Kampus", 5, 4)],
                           origin="Ivanovac", destination="Kampus")]
    result = match(elements, "IVANOVAC - OSIJEK (GAJEV TRG)", "GAJEV TRG - TVRĐAVICA")
    assert [m.relations for m in result.matched] == [(31, 33), (31,)]


def test_a_board_that_fits_two_lines_is_not_given_to_either():
    elements = [*bus_route(41, "3", "3: Čepin => Tenja", [("A", 0, 3), ("Trg Lj. Gaja", 2, 3),
                                                      ("B", 3, 3)], origin="Čepin"),
                *bus_route(42, "4", "4: Čepin => Briješće", [("C", 0, 4), ("Trg Lj. Gaja", 2, 4),
                                                             ("D", 3, 4)], origin="Čepin")]
    result = match(elements, "ČEPIN - OSIJEK (GAJEV TRG)")
    assert result.matched == []
    [one] = result.unmatched
    assert one.why == WHY_SEVERAL_LINES and one.lines == ("3", "4")


def test_another_companys_coach_never_takes_a_board():
    coach = bus_route(51, "N950", "Flixbus N950: Ivanovac => Gajev trg",
                      [("Ivanovac", 0, 3), ("Trg Lj. Gaja", 2, 3)], operator="Flixbus")
    result = match(coach, "IVANOVAC - OSIJEK (GAJEV TRG)")
    assert result.matched == [] and result.unmatched[0].why == WHY_NO_ROUTE
    assert result.lines_without_board == []  # nor does it count as a missing city line


def test_the_places_between_the_ends_are_not_checked():
    result = match(line_six(), "IVANOVAC - NIKAMO - TVRĐAVICA")
    assert [m.line for m in result.matched] == ["6"]


def test_a_round_trip_needs_the_place_at_two_different_stops():
    ring = bus_route(61, "8", "8: Jug II => Bolnica => Jug II",
                     [("Jug II / okretište", 0, 3), ("Toplana", 2, 3), ("Bolnica", 4, 3),
                      ("Jug II / okretište", 6, 3)], origin="Jug II", destination="Jug II")
    one_way = bus_route(62, "1", "1: Jug II => Opus Arena",
                        [("Jug II / okretište", 0, 4), ("Opus Arena", 4, 4)])
    result = match([*ring, *one_way], "JUG II - KBC- JUG II")
    assert [(m.line, m.relations) for m in result.matched] == [("8", (61,))]


def test_a_city_line_no_board_reached_is_listed():
    seven = bus_route(71, "7", "7: Drinska => Uske njive",
                      [("Drinska", 0, 5), ("Uske njive", 3, 5)])
    result = match([*line_six(), *seven], "IVANOVAC - OSIJEK (GAJEV TRG)")
    assert result.lines == ["6"] and result.lines_without_board == ["7"]


# ---------------------------------------------------------------- the real routes and the real page
@pytest.fixture(scope="module")
def real_network():
    elements = json.loads((FIXTURES / "osijek_bus_routes.json").read_text(encoding="utf-8"))
    return StreetMap.from_osm([elements]).bus


@pytest.fixture(scope="module")
def real_boards():
    data = json.loads((FIXTURES / "gpp_bus_boards.json").read_text(encoding="utf-8"))
    return list(enumerate(data["routes"], 1))


@pytest.fixture(scope="module")
def real_result(real_network, real_boards):
    return match_boards(real_network, real_boards)


def key(text):
    """A board name without spaces and brackets: GPP writes "(GAJEV TRG )" and "(GAJEV TRG)"."""
    return "".join(c for c in normalize(text) if c.isalnum())


def by_name(items):
    return {key(item.route): item for item in items}


MATCHED = {
    "BIJELO BRDO - OSIJEK (GAJEV TRG)": "5", "OSIJEK (GAJEV TRG) - BIJELO BRDO": "5",
    "BRIJEŠĆE - OSIJEK (GAJEV TRG)": "4", "OSIJEK (GAJEV TRG) - BRIJEŠĆE": "4",
    "ČEPIN - OSIJEK (GAJEV TRG)": "3", "OSIJEK (GAJEV TRG) - ČEPIN": "3",
    "GAJEV TRG - FILIPOVICA - BRIJEŠĆE": "4", "DRINSKA - USKE NJIVE - GAJEV TRG": "7",
    "GAJEV TRG - JUG II": "1", "IVANOVAC - OSIJEK (GAJEV TRG)": "6",
    "OSIJEK (GAJEV TRG) - IVANOVAC": "6", "JOSIPOVAC - OSIJEK (GAJEV TRG)": "5",
    "OSIJEK (GAJEV TRG) - JOSIPOVAC": "5", "JUG II - KBC- JUG II": "8",
    "JUG II - SJP ORAO / OPUS ARENA - VIJENAC PETROVE GORE": "1", "OPUS ARENA - JUG II": "1",
    "RETFALA - UL. HRVATSKE REPUBLIKE": "2", "GAJEV TRG - TVRĐAVICA": "6",
    "TVRĐAVICA - GAJEV TRG": "6", "VIJENAC PETROVE GORE - JUG II": "1",
}
NOT_MATCHED = {
    "ANTUNOVAC - OSIJEK (GAJEV TRG)": WHY_NO_ROUTE, "OSIJEK (GAJEV TRG) - ANTUNOVAC": WHY_NO_ROUTE,
    "BILJE - OSIJEK (GAJEV TRG)": WHY_NO_ROUTE, "OSIJEK (GAJEV TRG) - BILJE": WHY_NO_ROUTE,
    "BRIJEST - OSIJEK (GAJEV TRG)": WHY_NO_ROUTE, "OSIJEK (GAJEV TRG) - BRIJEST": WHY_NO_ROUTE,
    "FILIPOVICA - GAJEV TRG": WHY_NO_ROUTE, "GAJEV TRG - DRINSKA - USKE NJIVE": WHY_NO_ROUTE,
    "GAJEV TRG - OPUS ARENA - VIJENAC": WHY_NO_ROUTE, "KBC - JUG II": WHY_NO_ROUTE,
    "UL. HRVATSKE REPUBLIKE - RETFALA": WHY_NO_ROUTE, "SARVAŠ - OSIJEK": WHY_NO_ROUTE,
    "OSIJEK (GAJEV TRG) - SARVAŠ": WHY_NO_ROUTE,
    "OSIJEK (UL. HRVATSKE REPUBLIKE) - TENJA 4-3": WHY_SEVERAL_LINES,
    "TENJA 4-3 - OSIJEK (GAJEV TRG)": WHY_SEVERAL_LINES,
    "OSIJEK (GAJEV TRG) - TENJA 4-3": WHY_SEVERAL_LINES, "MIO - GAJEV TRG": WHY_SEVERAL_LINES,
    "GAJEV TRG -IND.ZONA NEMETIN, IND.ZONA NEMETIN-GAJEV TRG": WHY_TWO_LEGS,
}


def test_twenty_of_the_thirty_eight_real_boards_find_their_line(real_result):
    assert real_result.boards == 38
    assert {k: m.line for k, m in by_name(real_result.matched).items()} == {
        key(k): v for k, v in MATCHED.items()}


def test_the_other_eighteen_are_listed_each_with_its_reason(real_result):
    assert {k: u.why for k, u in by_name(real_result.unmatched).items()} == {
        key(k): v for k, v in NOT_MATCHED.items()}
    assert Counter(u.why for u in real_result.unmatched) == {
        WHY_NO_ROUTE: 13, WHY_SEVERAL_LINES: 4, WHY_TWO_LEGS: 1}


def test_every_city_line_of_the_map_got_at_least_one_board(real_result):
    assert real_result.lines == ["1", "2", "3", "4", "5", "6", "7", "8"]
    assert real_result.lines_without_board == []


def test_the_ambiguous_boards_name_the_lines_they_fit(real_result):
    unmatched = by_name(real_result.unmatched)
    assert unmatched[key("TENJA 4-3 - OSIJEK (GAJEV TRG)")].lines == ("3", "4")
    assert unmatched[key("MIO - GAJEV TRG")].lines == ("3", "4", "5")


def test_a_board_keeps_every_variant_of_its_line_that_fits(real_result):
    matched = by_name(real_result.matched)
    assert len(matched[key("IVANOVAC - OSIJEK (GAJEV TRG)")].relations) == 5
    assert len(matched[key("OSIJEK (GAJEV TRG) - IVANOVAC")].relations) == 2


# ---------------------------------------------------------------- saying it in Dnevnik
def add_boards(db, texts, mode="bus"):
    for text in texts:
        db.add(TimetableBoard(mode=mode, title=text, route=text))
    db.flush()


def entries(db):
    return db.scalars(select(Event).where(Event.kind == KIND).order_by(Event.id)).all()


def test_nothing_is_written_without_a_map_with_bus_routes_or_without_a_timetable(db):
    add_boards(db, ["IVANOVAC - OSIJEK (GAJEV TRG)"])
    assert report_bus_lines(db, StreetMap.from_osm([town()])) is None  # a map without buses
    assert report_bus_lines(db, SimpleNamespace(bus=None)) is None
    db.query(TimetableBoard).delete()
    assert report_bus_lines(db, streets_with(*line_six())) is None  # no timetable loaded yet
    add_boards(db, ["IVANOVAC - OSIJEK (GAJEV TRG)"], mode="tram")  # only the tram's boards
    assert report_bus_lines(db, streets_with(*line_six())) is None
    assert entries(db) == []


def test_the_result_is_written_once_and_again_only_when_it_changes(db):
    streets = streets_with(*line_six())
    add_boards(db, ["IVANOVAC - OSIJEK (GAJEV TRG)", "BILJE - OSIJEK (GAJEV TRG)"])
    first = report_bus_lines(db, streets)
    assert [m.line for m in first.matched] == ["6"] and len(first.unmatched) == 1
    report_bus_lines(db, streets)  # the same facts again: no second entry
    [entry] = entries(db)
    assert entry.data["boards"] == 2 and entry.data["matched"] == 1
    assert entry.data["lines"] == ["6"] and entry.data["why_counts"] == {WHY_NO_ROUTE: 1}
    add_boards(db, ["GAJEV TRG - TVRĐAVICA"])
    report_bus_lines(db, streets)
    assert len(entries(db)) == 2 and entries(db)[1].data["matched"] == 2


def event_text(data):
    return _describe(Event(kind=KIND, data=data), {}, {})


def test_the_dnevnik_line_says_what_fits_what_does_not_and_why(db):
    texts = json.loads((FIXTURES / "gpp_bus_boards.json").read_text(encoding="utf-8"))["routes"]
    add_boards(db, texts)
    elements = json.loads((FIXTURES / "osijek_bus_routes.json").read_text(encoding="utf-8"))
    report_bus_lines(db, StreetMap.from_osm([elements]))
    [entry] = entries(db)
    text, level = event_text(entry.data)
    assert text.startswith("Autobusne linije: s kartom je povezano 20 od 38 tablica voznog reda "
                           "(linije: 1, 2, 3, 4, 5, 6, 7, 8).")
    assert "Bez veze s kartom ostaje 18" in text and "nema takve linije na karti: 13" in text
    assert "pristaje uz više linija, pa ne pogađam koja: 4" in text
    assert "dvije vožnje u jednoj tablici: 1" in text and "i još 12." in text
    assert level == "info" and "bez voznog reda" not in text
    assert len(entry.data["unmatched"]) == 18  # all of them stay in the entry's data


def test_the_dnevnik_line_for_a_perfect_fit_is_ok_and_names_a_line_without_a_board():
    text, level = event_text({"boards": 2, "matched": 2, "lines": ["6"], "unmatched": [],
                              "unmatched_count": 0, "lines_without_board": ["7", "8"]})
    assert level == "ok" and "povezano 2 od 2" in text and "Bez veze" not in text
    assert text.endswith("Linije na karti bez voznog reda: 7, 8.")


# ---------------------------------------------------------------- when it is written
def test_a_map_download_says_which_city_lines_it_found_and_how_the_timetable_fits(
        db, tmp_path, monkeypatch):
    monkeypatch.setattr(mapstore, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setitem(osm_download.MIN_WAYS, "detailed", 5)
    monkeypatch.setitem(osm_download.MIN_WAYS, "main_roads", 5)
    mapstore.forget_street_map()
    map_download.reset_state()
    add_boards(db, ["ZAPAD - ISTOK", "BILJE - OSIJEK (GAJEV TRG)"])
    map_download.run_download(lambda: nullcontext(db), tmp_path, client=FakeServer().client(),
                              sleep=lambda _s: None)
    [done] = db.scalars(select(Event).where(Event.kind == "map_downloaded")).all()
    assert done.data["bus_lines"] == ["1"]
    [entry] = entries(db)
    assert entry.data["matched"] == 1 and entry.data["unmatched_count"] == 1
    newest = load_log(db).entries
    assert any("Gradske autobusne linije na karti: 1." in e.text for e in newest)
    assert any(e.text.startswith("Autobusne linije: s kartom je povezano 1 od 2") for e in newest)
    mapstore.forget_street_map()
    map_download.reset_state()


def test_a_matching_problem_never_makes_a_good_map_download_fail(db, monkeypatch, caplog):
    def boom(*_a, **_kw):
        raise RuntimeError("the timetable is broken")

    monkeypatch.setattr(map_download, "report_bus_lines", boom)
    map_download._report_bus_lines(db, object())  # logged, not raised
    assert "could not match the bus timetable" in caplog.text
    map_download._report_bus_lines(db, None)  # no map: nothing to say


def test_an_old_log_entry_of_a_download_reads_as_before():
    text, _level = _describe(
        Event(kind="map_downloaded", data={"bytes": 11_000_000, "rescored": 4}), {}, {})
    assert text == "Karta ulica je preuzeta (11 MB); ocjena je ponovno izračunata za 4 oglasa."


def test_a_map_without_city_bus_lines_says_so():
    text, _level = _describe(
        Event(kind="map_downloaded", data={"bytes": 1_000_000, "rescored": 0, "bus_lines": []}),
        {}, {})
    assert text.endswith("Na karti nema gradskih autobusnih linija.")


# Three more tests load GPP Osijek's timetable page through `FakeClient` (saved pages of
# web.gpp-osijek.com). Those pages are not republished in this public copy; the tests are in
# the author's private repository.
