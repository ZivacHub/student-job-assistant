"""Step 4.9 (G1): walking on a street map follows the streets, not a straight line."""

import pytest

import app.geo.streets as streets
from app.geo.streets import INF, StreetMap, walk_rule
from app.geo.travel import travel_minutes
from tests.street_fixtures import doc, line, pt, signal, town, way

WALK_MS = 4.8 / 3.6  # metres per second at the walking pace


def make(*docs) -> StreetMap:
    return StreetMap.from_osm(docs)


# ---------------------------------------------------------------- streets, not a straight line
def test_a_walk_takes_the_time_the_streets_take():
    sm = make(town())
    minutes = sm.minutes("walk", pt(2, 3), pt(8, 3))
    # down 300 m, across the bridge 600 m, up 300 m = 1200 m
    assert minutes == pytest.approx(1200 / WALK_MS / 60, abs=0.1)


def test_across_the_river_is_much_farther_than_the_old_straight_line_guess():
    sm = make(town())
    real = sm.minutes("walk", pt(2, 3), pt(8, 3))
    guess = travel_minutes(600 / 1000, "walk")  # 600 m in a straight line, x 1.3, at 5 km/h
    assert real > guess * 1.5


def test_the_river_is_crossed_only_where_there_is_a_bridge():
    no_bridge = [e for e in town()["elements"] if e["id"] != 2]
    sm = make(doc(*no_bridge))
    answer = sm.minutes("walk", pt(2, 3), pt(8, 3))
    assert answer is None or answer == INF  # never a made-up number


def test_a_walk_on_the_same_bank_does_not_need_the_bridge():
    sm = make(town())
    assert sm.minutes("walk", pt(0, 3), pt(4, 3)) == pytest.approx(400 / WALK_MS / 60, abs=0.1)


# ---------------------------------------------------------------- lights and crossings
def town_with_a_footway_down() -> list[dict]:
    """The town where the way down from the west street is a footway (a crossing can sit on it)."""
    others = [e for e in town()["elements"] if e["id"] != 6]
    return [*others, way(6, line(2, 0, 2, 3), highway="footway")]


def walk_a_to_b(*extra) -> float:
    return make(doc(*town_with_a_footway_down(), *extra)).minutes("walk", pt(2, 3), pt(8, 3))


def test_a_pedestrian_crossing_with_lights_makes_a_walker_wait():
    lit = walk_a_to_b(signal(2, 1, highway="crossing", crossing="traffic_signals"))
    assert lit - walk_a_to_b() == pytest.approx(20 / 60, abs=0.01)


def test_a_marked_crossing_costs_a_walker_a_few_seconds():
    zebra = walk_a_to_b(signal(2, 1, highway="crossing", crossing="zebra"))
    assert zebra - walk_a_to_b() == pytest.approx(6 / 60, abs=0.01)


def test_a_walker_on_a_road_does_not_wait_at_lights_made_for_cars():
    plain = make(town()).minutes("walk", pt(2, 3), pt(8, 3))
    lit = make(town(), doc(signal(2, 1))).minutes("walk", pt(2, 3), pt(8, 3))  # a road
    assert lit == pytest.approx(plain)


# ---------------------------------------------------------------- the ground under the feet
def test_steps_are_slower_than_a_street():
    sm = make(doc(way(1, line(0, 0, 3, 0), highway="steps")))
    assert sm.minutes("walk", pt(0, 0), pt(3, 0)) == pytest.approx(300 / (2.4 / 3.6) / 60, abs=0.1)


def test_unpaved_ground_is_slower_than_a_paved_street():
    paved = walk_rule({"highway": "footway", "surface": "asphalt"}).speed_ms
    sand = walk_rule({"highway": "footway", "surface": "sand"}).speed_ms
    grass = walk_rule({"highway": "path", "surface": "grass"}).speed_ms
    assert sand < grass < paved


@pytest.mark.parametrize("tags", [
    {"highway": "motorway"},
    {"highway": "trunk"},  # no pavement unless the map says walking is fine
    {"highway": "residential", "foot": "no"},
    {"highway": "service", "access": "private"},
    {"highway": "footway", "indoor": "yes"},
    {"highway": "construction"},
    {"highway": "proposed"},
])
def test_ways_a_pedestrian_may_not_use_are_left_out(tags):
    assert walk_rule(tags) is None


@pytest.mark.parametrize("tags", [
    {"highway": "trunk", "foot": "yes"},
    {"highway": "service", "access": "private", "foot": "yes"},
    {"highway": "footway"},
    {"highway": "track"},
])
def test_ways_a_pedestrian_may_use_are_kept(tags):
    assert walk_rule(tags) is not None


# ---------------------------------------------------------------- where the map ends
def test_a_point_outside_the_map_gets_no_answer():
    sm = make(town())
    assert sm.minutes("walk", pt(2, 3), pt(500, 500)) is None
    assert sm.minutes("walk", pt(500, 500), pt(2, 3)) is None


def test_a_point_a_little_off_the_street_pays_for_the_walk_to_it():
    sm = make(town())
    on_street = sm.minutes("walk", pt(2, 3), pt(8, 3))
    off_street = sm.minutes("walk", pt(2, 3), pt(8, 3.5))  # 50 m north of the street
    assert off_street - on_street == pytest.approx(50 * 1.25 / WALK_MS / 60, abs=0.02)


def test_a_point_too_far_from_any_street_gets_no_answer():
    sm = make(town())
    assert sm.minutes("walk", pt(2, 3), pt(8, 3 + 8)) is None  # about 800 m from the nearest street


def test_a_separate_little_network_is_never_taken_for_the_street_next_to_a_job():
    island = way(90, line(40, 40, 42, 40))  # 4 km away from the town and joined to nothing
    sm = make(town(), doc(island))
    assert sm.minutes("walk", pt(2, 3), pt(41, 40)) is None


# ---------------------------------------------------------------- results are kept
def test_the_same_question_is_answered_from_memory():
    sm = make(town())
    first = sm.field("walk", pt(2, 3))
    assert sm.field("walk", pt(2, 3)) is first
    assert sm.field("walk", pt(2.0000001, 3)) is first  # the same place, rounded


def test_memory_is_limited(monkeypatch):
    monkeypatch.setattr(streets, "MAX_FIELDS", 2)
    sm = make(town())
    first = sm.field("walk", pt(0, 0))
    sm.field("walk", pt(2, 0))
    sm.field("walk", pt(4, 0))
    assert sm.field("walk", pt(0, 0)) is not first  # the oldest one was dropped and recomputed


# ---------------------------------------------------------------- the area that can be reached
def test_the_reachable_area_follows_the_streets_and_stops_at_the_river():
    sm = make(town())
    points = sm.reach_points("walk", pt(2, 3), 6)  # 6 minutes = 480 m
    assert points is not None
    cells = {(round((lon - pt(0, 0)[1]) / (pt(1, 0)[1] - pt(0, 0)[1])),
              round((lat - pt(0, 0)[0]) / (pt(0, 1)[0] - pt(0, 0)[0]))) for lat, lon in points}
    assert (3, 0) in cells  # 400 m away along the streets
    assert (4, 0) not in cells  # 500 m: just too far
    assert (8, 3) not in cells  # 600 m in a straight line, but the river is in the way


def test_a_mode_the_map_does_not_model_is_not_answered():
    sm = make(town())
    assert sm.minutes("bus", pt(2, 3), pt(8, 3)) is None
