"""Step 4.9 (G3): the tram estimate (D28): walk to a stop, wait, ride, maybe change, walk on."""

import pytest

from app.geo.streets import INF, StreetMap
from app.geo.tram import RIDE_DETOUR, TRAM_KMH, WAIT_S, TramNetwork
from tests.street_fixtures import doc, line, pt, tram_route, way

WALK_MS = 4.8 / 3.6
TRAM_MS = TRAM_KMH / 3.6


def ride_s(metres: float) -> float:
    return metres * RIDE_DETOUR / TRAM_MS


def grid_streets(size: int = 12) -> list[dict]:
    """Streets on every grid line, i and j from 0 to `size` (100 m apart)."""
    ways, wid = [], 1
    for k in range(size + 1):
        ways.append(way(wid, line(0, k, size, k)))
        ways.append(way(wid + 1, line(k, 0, k, size)))
        wid += 2
    return ways


def tram_lines() -> list[dict]:
    """Line 1 runs west-east along j=6, line 2 south-north along i=6; both have a stop at (6, 6)."""
    one = [(i, 6) for i in range(0, 13, 3)]
    two = [(6, j) for j in range(0, 13, 3)]
    return [
        tram_route(1, "1", one), tram_route(2, "1", one[::-1]),
        tram_route(3, "2", two), tram_route(4, "2", two[::-1]),
    ]


def make(extra_streets=()) -> StreetMap:
    return StreetMap.from_osm([doc(*grid_streets(), *extra_streets, *tram_lines())])


# ---------------------------------------------------------------- the estimate
def test_a_tram_trip_is_a_wait_plus_a_ride():
    sm = make()
    minutes = sm.minutes("tram", pt(0, 6), pt(12, 6))
    assert minutes == pytest.approx((WAIT_S + ride_s(1200)) / 60, abs=0.05)  # about 10.9 min
    # the same way on foot is 15 minutes: the tram wins over this distance
    assert minutes < sm.minutes("walk", pt(0, 6), pt(12, 6))


def test_the_walks_to_and_from_the_stops_are_added():
    sm = make()
    # 200 m off the street before the first stop (a walk to the street costs 1.25 x the distance),
    # and 100 m along the street after the last stop
    minutes = sm.minutes("tram", pt(-2, 6), pt(12, 5))
    expected = (200 * 1.25 / WALK_MS + WAIT_S + ride_s(1200) + 100 / WALK_MS) / 60
    assert minutes == pytest.approx(expected, abs=0.1)


def test_a_plain_walk_is_never_counted_as_a_tram_trip():
    sm = make()
    walking = sm.minutes("walk", pt(1, 0), pt(1, 1))  # 100 m: about a minute and a quarter
    tram = sm.minutes("tram", pt(1, 0), pt(1, 1))
    assert walking < 1.5
    assert tram > WAIT_S / 60  # at least the wait for a tram, because a tram ride is required


def test_changing_to_the_other_line_costs_a_second_wait():
    # line 1 runs along j=6 for 2.4 km, line 2 crosses it at (18, 6) and runs north
    one = [(i, 6) for i in range(0, 25, 3)]
    two = [(18, j) for j in range(0, 25, 3)]
    routes = [tram_route(1, "1", one), tram_route(2, "1", one[::-1]),
              tram_route(3, "2", two), tram_route(4, "2", two[::-1])]
    sm = StreetMap.from_osm([doc(*grid_streets(24), *routes)])
    # ride line 1 for 1.8 km, change, ride line 2 for 1.2 km: two waits and two rides
    minutes = sm.minutes("tram", pt(0, 6), pt(18, 18))
    expected = (WAIT_S + ride_s(1800) + WAIT_S + ride_s(1200)) / 60  # about 24.2 min
    assert minutes == pytest.approx(expected, abs=0.1)
    assert minutes < (1800 + 1200) / WALK_MS / 60  # still faster than walking all of it (37.5 min)


def test_a_short_last_stretch_is_walked_instead_of_waiting_for_the_second_tram():
    sm = make()
    # from the shared stop to (6,12) is 600 m: walking (7.5 min) beats waiting 6 min + riding 2.4
    # (and walking to the shared stop first would cost more than the first tram ride does)
    minutes = sm.minutes("tram", pt(0, 6), pt(6, 12))
    assert minutes == pytest.approx((WAIT_S + ride_s(600) + 600 / WALK_MS) / 60, abs=0.1)


def test_a_far_place_with_no_stop_within_a_half_hour_walk_has_no_tram_trip():
    far = [way(50, line(0, 20, 60, 20)), way(51, line(0, 12, 0, 20))]  # a lone street, 6 km out
    sm = make(far)
    assert sm.minutes("tram", pt(60, 20), pt(6, 6)) == INF


def test_without_any_tram_data_the_map_cannot_answer():
    sm = StreetMap.from_osm([doc(*grid_streets())])
    assert sm.tram is None
    assert sm.minutes("tram", pt(0, 6), pt(12, 6)) is None


# ---------------------------------------------------------------- how the stops are read
def test_the_two_ends_of_a_route_are_for_getting_on_and_for_getting_off():
    cells = [(0, 6), (3, 6), (6, 6), (9, 6), (12, 6)]
    route = tram_route(1, "1", cells, roles={0: "stop_entry_only", 4: "stop_exit_only"})
    net = TramNetwork.from_relations([route])
    first, last = net.stops[0], net.stops[-1]
    assert (first.can_board, first.can_alight) == (True, False)
    assert (last.can_board, last.can_alight) == (False, True)
    assert net.lines == ["1"]


def test_members_that_are_not_stops_are_ignored():
    route = tram_route(1, "1", [(0, 6), (3, 6)])
    route["members"].append({"type": "way", "ref": 5, "role": ""})
    platform = {"type": "node", "ref": 9, "role": "platform", "lat": 45.5, "lon": 18.7}
    route["members"].append(platform)
    net = TramNetwork.from_relations([route])
    assert len(net.stops) == 2


def test_stops_added_out_of_order_at_the_end_of_a_route_are_not_a_ride():
    # as in the real data: after the last real stop the list continues with a stop 3 km away
    cells = [(0, 6), (3, 6), (6, 6), (36, 6)]
    net = TramNetwork.from_relations([tram_route(1, "1", cells)])
    arrivals = net._ride_times({0: 0.0})  # boarded at the first stop
    assert set(arrivals) == {1, 2}  # the two stops after it; the far one cannot be ridden to
    assert arrivals[2] == pytest.approx(ride_s(600), rel=0.01)


def test_both_lines_are_known():
    net = make().tram
    assert net.lines == ["1", "2"]


# ---------------------------------------------------------------- the area on the map
def test_the_tram_reaches_farther_than_walking_in_the_same_time():
    sm = make()
    end = pt(12, 6)

    def near(points):
        return any(abs(lat - end[0]) < 1e-6 and abs(lon - end[1]) < 1e-6 for lat, lon in points)

    assert near(sm.reach_points("tram", pt(0, 6), 12))  # 10.9 minutes by tram
    assert not near(sm.reach_points("walk", pt(0, 6), 12))  # 15 minutes on foot
