"""Step 4.12 (D41): the bus lines of the street map: routes with their stops in order.

Hand-made routes for the rules, and a trimmed copy of the real answer for the map server
(tests/fixtures/osm_bus/, 29 routes and their 292 stops, (c) OpenStreetMap contributors, ODbL).
"""

import json
from pathlib import Path

import pytest

from app.geo.bus import MAX_NAME_CHARS, is_local
from app.geo.streets import StreetMap
from tests.street_fixtures import bus_route, doc, pt, town

REAL_FILE = Path(__file__).parent / "fixtures" / "osm_bus" / "osijek_bus_routes.json"


def network(*elements):
    return StreetMap.from_osm([doc(*town()["elements"], *elements)]).bus


def stops(name_cells, **kwargs):
    return bus_route(11, "6", "6: A => B", name_cells, **kwargs)


# ---------------------------------------------------------------- reading a route
def test_a_route_keeps_its_stops_in_the_order_of_the_relation():
    net = network(*stops([("Zapad", 0, 3), ("Sredina", 2, 3), ("Istok", 3, 3)],
                         origin="Zapad", destination="Istok"))
    [route] = net.routes
    assert [s.name for s in route.stops] == ["Zapad", "Sredina", "Istok"]
    assert [s.order for s in route.stops] == [0, 1, 2]
    assert (route.stops[1].lat, route.stops[1].lon) == pt(2, 3)
    assert route.ref == "6" and route.name == "6: A => B"
    assert route.origin == "Zapad" and route.destination == "Istok" and route.relation == 11


def test_a_street_piece_among_the_members_is_ignored():
    """`bus_route` puts a way member first: it must not become a stop or break the order."""
    [route] = network(*stops([("Zapad", 0, 3), ("Istok", 3, 3)])).routes
    assert [s.name for s in route.stops] == ["Zapad", "Istok"]


def test_stop_roles_with_a_suffix_count_and_other_roles_do_not():
    elements = stops([("Prvi", 0, 3), ("Drugi", 1, 3), ("Treci", 2, 3), ("Cetvrti", 3, 3)])
    relation = elements[0]
    relation["members"][1]["role"] = "stop_entry_only"
    relation["members"][2]["role"] = "platform_exit_only"
    relation["members"][3]["role"] = ""  # no role: not a stop
    relation["members"][4]["role"] = "via"
    [route] = network(*elements).routes
    assert [s.name for s in route.stops] == ["Prvi", "Drugi"]


def test_a_stop_the_answer_has_no_position_for_is_left_out_and_an_empty_route_is_dropped():
    elements = stops([("Zapad", 0, 3), ("Istok", 3, 3)])
    del elements[1]  # the node of "Zapad" is missing from the answer
    [route] = network(*elements).routes
    assert [s.name for s in route.stops] == ["Istok"]
    lone = bus_route(12, "9", "9: X", [("Nikamo", 1, 1)])
    del lone[1]
    assert network(lone[0]) is None  # a route without a single known stop is not a route


def test_no_bus_relations_means_no_bus_network():
    assert StreetMap.from_osm([town()]).bus is None


def test_an_unnamed_stop_is_not_a_stop_and_names_are_clipped():
    elements = stops([("Zapad", 0, 3), ("x" * 500, 3, 3)])
    elements[2]["tags"].pop("name")
    elements[0]["tags"]["name"] = "n" * 500
    [route] = network(*elements).routes
    assert [s.name for s in route.stops] == ["Zapad"]
    assert len(route.name) == MAX_NAME_CHARS


# ---------------------------------------------------------------- whose routes they are
@pytest.mark.parametrize("tags, expected", [
    ({"operator": "GPP Osijek"}, True),
    ({"network": "GPP Osijek"}, True),
    ({"operator": "gpp  osijek"}, True),
    ({"operator": "Flixbus", "network": "Flixbus"}, False),
    ({"operator": "Centrotrans", "network": "Flixbus"}, False),
    ({}, False),
])
def test_only_the_city_bus_company_counts_as_local(tags, expected):
    assert is_local(tags) is expected


def test_other_companies_routes_are_kept_but_not_local():
    net = network(*stops([("Zapad", 0, 3), ("Istok", 3, 3)]),
                  *bus_route(12, "N950", "Flixbus N950: A => B", [("Kolodvor", 5, 3),
                                                                  ("Granica", 9, 3)],
                             operator="Flixbus"))
    assert sorted(r.ref for r in net.routes) == ["6", "N950"]
    assert [r.ref for r in net.local_routes] == ["6"]
    assert net.lines == ["6"]


def test_lines_are_listed_by_number_not_alphabetically():
    elements = [e for ref, rel_id in (("10", 21), ("2", 22), ("1", 23))
                for e in bus_route(rel_id, ref, f"{ref}: A => B", [("A", 0, 3), ("B", 3, 3)])]
    assert network(*elements).lines == ["1", "2", "10"]


# ---------------------------------------------------------------- the real answer
@pytest.fixture(scope="module")
def real():
    return StreetMap.from_osm([json.loads(REAL_FILE.read_text(encoding="utf-8"))]).bus


def test_the_real_answer_gives_the_citys_eight_lines_and_the_coaches_apart(real):
    assert len(real.routes) == 29 and len(real.local_routes) == 23
    assert real.lines == ["1", "2", "3", "4", "5", "6", "7", "8"]
    others = {r.ref for r in real.routes if not r.local}
    assert others == {"966", "N950", "N1970"}  # the Flixbus coaches through Osijek


def test_the_real_line_six_has_its_variants_each_with_ordered_stops(real):
    six = [r for r in real.routes if r.ref == "6"]
    assert len(six) == 8
    to_zoo = next(r for r in six if r.relation == 14459731)
    assert to_zoo.stops[0].name == "Okretište Čepinska ul."
    assert to_zoo.stops[-1].name == "Okretište - ZOO"
    names = [s.name for s in to_zoo.stops]
    assert names.index("Ivanovac 1 / Duga ul.") < names.index("Trg Lj. Gaja") < names.index(
        "Tvrđavica 1")
    for route in real.local_routes:  # every stop is a real place in and around Osijek
        assert all(45.4 < s.lat < 45.7 and 18.4 < s.lon < 18.9 for s in route.stops)
