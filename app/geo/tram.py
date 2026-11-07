"""The tram on top of the street graph (plan step 4.9, D28).

Osijek has two tram lines run by GPP Osijek. OpenStreetMap has each line as a route with its stops
in order, but no timetable exists in the data, so a tram trip is an ESTIMATE:

    walk to a stop  +  wait (about 6 minutes on average)  +  ride (about 17 km/h on average)
    (+ a change at a shared stop, with another wait)  +  walk from the stop to the job.

A plain walk is never counted as a tram trip: at least one ride is part of every answer.
"""

from __future__ import annotations

import heapq
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.geo.streets import (
    INF,
    OVERHEAD_S,
    SNAP_WALK_FACTOR,
    WALK_KMH,
    Field,
    _kmh,
    dijkstra,
    distance_m,
)

if TYPE_CHECKING:
    from app.geo.streets import StreetMap

TRAM_KMH = 17.0  # average speed including the time standing at stops
WAIT_S = 6 * 60.0  # average wait for a tram (no timetable is available)
RIDE_DETOUR = 1.15  # the track between two stops is not a straight line
TRANSFER_RADIUS_M = 150.0  # stops of different lines this close count as one place to change
# Two stops listed one after the other farther apart than this are not a ride: in the real data a
# route's list sometimes ends with a few stops added out of order (a jump of 3 to 5 km).
MAX_STOP_GAP_M = 1500.0
MAX_WALK_TO_STOP_S = 30 * 60.0
_BOARD_ROLES = {"stop", "stop_entry_only"}
_ALIGHT_ROLES = {"stop", "stop_exit_only"}
_STOP_ROLES = _BOARD_ROLES | _ALIGHT_ROLES


@dataclass(frozen=True)
class Stop:
    line: str  # the line number as printed on the tram ("1", "2")
    route: int  # OpenStreetMap relation id: one direction of one line
    order: int  # position along the route
    lat: float
    lon: float
    can_board: bool
    can_alight: bool


class TramNetwork:
    def __init__(self, stops: list[Stop]):
        self.stops = stops
        walk_ms = _kmh(WALK_KMH)
        # states per stop i:  ("B", i) waiting at the stop   ("T0", i) just boarded
        #                      ("T1", i) on the tram after at least one ride   ("E", i) got off
        self.edges: dict[tuple[str, int], list[tuple[tuple[str, int], float]]] = {}

        def add(a, b, seconds):
            self.edges.setdefault(a, []).append((b, seconds))

        by_route: dict[int, list[int]] = {}
        for i, s in enumerate(stops):
            by_route.setdefault(s.route, []).append(i)
            if s.can_board:
                add(("B", i), ("T0", i), 0.0)
            if s.can_alight:
                add(("T1", i), ("E", i), 0.0)
        for indices in by_route.values():
            indices.sort(key=lambda i: stops[i].order)
            for a, b in zip(indices, indices[1:], strict=False):
                metres = distance_m(stops[a].lat, stops[a].lon, stops[b].lat, stops[b].lon)
                if metres > MAX_STOP_GAP_M:
                    continue
                ride = metres * RIDE_DETOUR / _kmh(TRAM_KMH)
                add(("T0", a), ("T1", b), ride)
                add(("T1", a), ("T1", b), ride)
        for i, a in enumerate(stops):  # changing to another line at a place both lines serve
            for j, b in enumerate(stops):
                if i == j or a.line == b.line or not (a.can_alight and b.can_board):
                    continue
                metres = distance_m(a.lat, a.lon, b.lat, b.lon)
                if metres <= TRANSFER_RADIUS_M:
                    add(("E", i), ("B", j), metres * SNAP_WALK_FACTOR / walk_ms + WAIT_S)

    @classmethod
    def from_relations(cls, relations: Iterable[dict]) -> TramNetwork | None:
        stops: list[Stop] = []
        for rel in relations:
            line = str((rel.get("tags") or {}).get("ref") or "?")
            order = 0
            for member in rel.get("members", []):
                role = member.get("role", "")
                if member.get("type") != "node" or role not in _STOP_ROLES:
                    continue
                if "lat" not in member:
                    continue
                stops.append(Stop(line, rel["id"], order, member["lat"], member["lon"],
                                  role in _BOARD_ROLES, role in _ALIGHT_ROLES))
                order += 1
        return cls(stops) if stops else None

    @property
    def lines(self) -> list[str]:
        return sorted({s.line for s in self.stops})

    # ---------------------------------------------------------------- the answer for one origin
    def _ride_times(self, boarding: dict[int, float]) -> dict[int, float]:
        """Seconds at which one can step off at each stop, given when one can board where."""
        best: dict[tuple[str, int], float] = {}
        heap: list[tuple[float, tuple[str, int]]] = []
        for i, seconds in boarding.items():
            state = ("B", i)
            best[state] = seconds
            heap.append((seconds, state))
        heapq.heapify(heap)
        while heap:
            d, state = heapq.heappop(heap)
            if d > best.get(state, INF):
                continue
            for nxt, w in self.edges.get(state, ()):
                nd = d + w
                if nd < best.get(nxt, INF):
                    best[nxt] = nd
                    heapq.heappush(heap, (nd, nxt))
        return {state[1]: d for state, d in best.items() if state[0] == "E"}

    def compute_field(self, sm: StreetMap, origin: tuple[float, float]) -> Field | None:
        walk = sm.graphs.get("walk")
        start = sm.snap("walk", *origin)
        if walk is None or start is None:
            return None
        node, metres = start
        walk_ms = _kmh(WALK_KMH)
        from_origin = dijkstra(walk.adj, {node: metres * SNAP_WALK_FACTOR / walk_ms})

        stop_nodes: dict[int, tuple[int, float]] = {}
        boarding: dict[int, float] = {}
        for i, s in enumerate(self.stops):
            snapped = sm.snap("walk", s.lat, s.lon)
            if snapped is None:
                continue
            stop_nodes[i] = (snapped[0], snapped[1] * SNAP_WALK_FACTOR / walk_ms)
            if s.can_board:
                walk_s = from_origin[snapped[0]] + stop_nodes[i][1]
                if walk_s <= MAX_WALK_TO_STOP_S:
                    boarding[i] = walk_s + WAIT_S
        arrivals = self._ride_times(boarding)

        starts: dict[int, float] = {}
        for i, seconds in arrivals.items():
            if i not in stop_nodes:
                continue
            walk_node, walk_s = stop_nodes[i]
            total = seconds + walk_s
            if total < starts.get(walk_node, INF):
                starts[walk_node] = total
        dist = array("d", dijkstra(walk.adj, starts) if starts else [INF] * len(walk.adj))
        return Field(sm, "tram", "walk", dist, OVERHEAD_S["tram"])
