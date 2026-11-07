"""Step 4.9 (G2): cycling and driving follow their own rules on the same street map."""

import pytest

from app.geo.streets import StreetMap, bike_rule, car_rule, walk_rule
from tests.street_fixtures import doc, line, node_id, pt, signal, town, way

WALK_MS = 4.8 / 3.6
CAR_RESIDENTIAL_MS = 30 / 3.6
BIKE_RESIDENTIAL_MS = 15 / 3.6


def make(*docs) -> StreetMap:
    return StreetMap.from_osm(docs)


def town_with_one_way_v2(**tags) -> StreetMap:
    """The town where street 6 (the way down from the west street) gets extra tags."""
    others = [e for e in town()["elements"] if e["id"] != 6]
    return make(doc(*others, way(6, line(2, 0, 2, 3), **tags)))


# ---------------------------------------------------------------- the end of the trip
def test_each_way_of_travelling_adds_its_own_time_at_the_end():
    sm = make(doc(way(1, line(0, 0, 10, 0))))  # one straight street, 1000 m
    a, b = pt(0, 0), pt(10, 0)
    assert sm.minutes("walk", a, b) == pytest.approx(1000 / WALK_MS / 60, abs=0.05)
    assert sm.minutes("bike", a, b) == pytest.approx(1000 / BIKE_RESIDENTIAL_MS / 60 + 2, abs=0.05)
    assert sm.minutes("car", a, b) == pytest.approx(1000 / CAR_RESIDENTIAL_MS / 60 + 5, abs=0.05)


# ---------------------------------------------------------------- one-way streets
def test_a_car_follows_a_one_way_street_but_a_walker_does_not():
    free = make(town())
    oneway = town_with_one_way_v2(oneway="yes")  # north only
    a, b = pt(2, 3), pt(8, 3)
    # south is closed, so the car goes round by the other street on the west bank:
    # 200 m west + 300 m down + 800 m east + 300 m up = 1600 m instead of 1200 m
    direct = 1200 / CAR_RESIDENTIAL_MS / 60 + 5
    round_about = 1600 / CAR_RESIDENTIAL_MS / 60 + 5
    assert free.minutes("car", a, b) == pytest.approx(direct, abs=0.05)
    assert oneway.minutes("car", a, b) == pytest.approx(round_about, abs=0.05)
    assert oneway.minutes("walk", a, b) == pytest.approx(free.minutes("walk", a, b))


def test_the_one_way_street_is_open_in_its_own_direction():
    sm = town_with_one_way_v2(oneway="yes")
    north = sm.minutes("car", pt(2, 0), pt(2, 3))  # with the arrow: 300 m
    south = sm.minutes("car", pt(2, 3), pt(2, 0))  # against it: 200 + 300 + 200 = 700 m round
    assert north == pytest.approx(300 / CAR_RESIDENTIAL_MS / 60 + 5, abs=0.05)
    assert south == pytest.approx(700 / CAR_RESIDENTIAL_MS / 60 + 5, abs=0.05)


def test_a_reversed_one_way_street_points_the_other_way():
    sm = town_with_one_way_v2(oneway="-1")  # south only
    assert sm.minutes("car", pt(2, 3), pt(2, 0)) == pytest.approx(
        300 / CAR_RESIDENTIAL_MS / 60 + 5, abs=0.05)


def test_roundabouts_and_motorways_are_one_way_by_themselves():
    assert car_rule({"highway": "residential", "junction": "roundabout"}).oneway == 1
    assert car_rule({"highway": "motorway"}).oneway == 1
    explicit = {"highway": "residential", "oneway": "no", "junction": "roundabout"}
    assert car_rule(explicit).oneway == 0


def test_a_bike_may_go_against_a_one_way_street_when_the_map_says_so():
    assert bike_rule({"highway": "residential", "oneway": "yes"}).oneway == 1
    both = {"highway": "residential", "oneway": "yes"}
    assert bike_rule({**both, "oneway:bicycle": "no"}).oneway == 0
    assert bike_rule({**both, "cycleway": "opposite_lane"}).oneway == 0


# ---------------------------------------------------------------- lights
def light(direction=None, **kw):
    """Traffic lights on the way down from the west street (drawn from (2,0) up to (2,3))."""
    tags = {"highway": "traffic_signals", **kw}
    if direction:
        tags["traffic_signals:direction"] = direction
    return signal(2, 1, **tags)


def with_light(*nodes) -> StreetMap:
    return make(town(), doc(*nodes))


def drive(sm, a, b, mode="car"):
    return sm.minutes(mode, pt(*a), pt(*b))


def test_a_car_waits_only_at_the_lights_facing_it():
    plain = make(town())
    north_up, south_down = (2, 0), (2, 3)
    cases = (("forward", True, False), ("backward", False, True), ("both", True, True),
             (None, True, True))  # (what the map says, hits northbound, hits southbound)
    for facing, hits_northbound, hits_southbound in cases:
        lit = with_light(light(facing))
        up = drive(lit, north_up, south_down) - drive(plain, north_up, south_down)
        down = drive(lit, south_down, north_up) - drive(plain, south_down, north_up)
        assert up == pytest.approx(20 / 60 if hits_northbound else 0, abs=0.01), facing
        assert down == pytest.approx(20 / 60 if hits_southbound else 0, abs=0.01), facing


def test_lights_for_pedestrians_hold_a_car_only_briefly():
    plain = drive(make(town()), (2, 3), (2, 0))
    signals = drive(with_light(signal(2, 1, highway="crossing", crossing="traffic_signals")),
                    (2, 3), (2, 0))
    zebra = drive(with_light(signal(2, 1, highway="crossing", crossing="zebra")), (2, 3), (2, 0))
    assert signals - plain == pytest.approx(4 / 60, abs=0.01)
    assert zebra - plain == pytest.approx(1 / 60, abs=0.01)


def test_a_cyclist_waits_at_lights_too():
    plain = drive(make(town()), (2, 3), (2, 0), "bike")
    lit = drive(with_light(light()), (2, 3), (2, 0), "bike")
    assert lit - plain == pytest.approx(15 / 60, abs=0.01)


# ---------------------------------------------------------------- who may use what
def test_footways_are_closed_to_cars():
    spur = way(20, line(8, 3, 8, 4), highway="footway")  # a footway north of the east street
    sm = make(town(), doc(spur))
    assert car_rule({"highway": "footway"}) is None
    snapped = sm.snap("car", *pt(8, 4))  # the footway end is not a place a car can go to
    assert snapped is not None and snapped[0] == sm.ids[node_id(8, 3)]
    assert sm.snap("walk", *pt(8, 4))[0] == sm.ids[node_id(8, 4)]  # but a walker can


def test_motorways_are_open_to_cars_only():
    assert car_rule({"highway": "motorway"}) is not None
    assert walk_rule({"highway": "motorway"}) is None
    assert bike_rule({"highway": "motorway"}) is None
    assert bike_rule({"highway": "trunk"}) is not None  # an ordinary main road may be cycled


@pytest.mark.parametrize("tags", [
    {"highway": "residential", "access": "private"},
    {"highway": "residential", "motor_vehicle": "no"},
    {"highway": "service", "service": "driveway"},
    {"highway": "track"},
    {"highway": "pedestrian"},
    {"highway": "cycleway"},
])
def test_ways_a_car_may_not_use_are_left_out(tags):
    assert car_rule(tags) is None


@pytest.mark.parametrize("tags", [
    {"highway": "residential", "bicycle": "no"},
    {"highway": "motorway_link"},
    {"highway": "service", "access": "private"},
])
def test_ways_a_bike_may_not_use_are_left_out(tags):
    assert bike_rule(tags) is None


def test_a_bike_goes_slowly_where_riding_is_not_marked_as_allowed():
    slow = bike_rule({"highway": "footway"}).speed_ms
    shared = bike_rule({"highway": "footway", "bicycle": "designated"}).speed_ms
    steps = bike_rule({"highway": "steps"}).speed_ms
    assert slow == pytest.approx(8 / 3.6)  # between walking and riding on a street
    assert steps < WALK_MS < slow < shared
    cycleway = bike_rule({"highway": "cycleway"}).speed_ms
    assert cycleway > bike_rule({"highway": "residential"}).speed_ms


# ---------------------------------------------------------------- how fast a car really goes
@pytest.mark.parametrize("highway, maxspeed, kmh", [
    ("residential", "30", 24.0),  # a car does not keep to the limit through junctions
    ("primary", "90", 72.0),
    ("primary", "130", 100.0),  # never faster than 100 on average
    ("residential", "5", 10.0),  # never slower than a walking-ish 10
    ("residential", "50 mph", 50 * 1.609 * 0.8),
    ("residential", "walk", 30.0),  # not a number: use the road type
    ("residential", "none", 30.0),
    ("primary", None, 50.0),
    ("motorway", None, 100.0),
    ("living_street", None, 10.0),
])
def test_car_speed_comes_from_the_limit_or_the_road_type(highway, maxspeed, kmh):
    tags = {"highway": highway}
    if maxspeed is not None:
        tags["maxspeed"] = maxspeed
    assert car_rule(tags).speed_ms * 3.6 == pytest.approx(kmh, rel=0.001)


def test_a_far_door_off_the_mapped_roads_is_reached_at_car_speed_not_on_foot():
    # a main road of 20 km; the job is 1.5 km beside it, where only main roads are mapped
    sm = make(doc(way(1, line(0, 0, 200, 0), highway="primary")))
    near = sm.minutes("car", pt(0, 0), pt(100, 0))
    far = sm.minutes("car", pt(0, 0), pt(100, 15))
    assert far - near == pytest.approx(1500 * 1.3 / (25 / 3.6) / 60, abs=0.2)  # about 4.7 min
