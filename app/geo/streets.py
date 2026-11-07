"""Real street travel times, computed on this PC from OpenStreetMap data (plan step 4.9, D27).

The data is a one-time download (see `mapstore.py`), kept in `data/maps/`. From it three street
graphs are built (walking, cycling, car), and `tram.py` adds the tram on top. Nothing here talks
to the internet, and no place of Ivan's ever leaves the computer.

What a graph knows (the things a straight line cannot):
- a river or railway is crossed only where a bridge, tunnel or crossing really connects the two
  sides (a way is connected to another only through a point they share);
- one-way streets bind cars (and, with the usual exceptions, bikes) but not walkers;
- traffic lights and marked crossings cost waiting time;
- steps, tracks and unpaved ground are slower than a paved street;
- motorways are closed to walkers and bikes, footways are closed to cars.

`StreetMap.minutes(mode, origin, destination)` answers:
- a number of minutes,
- `INF`: the map covers both ends but there is no way by this mode,
- `None`: the map cannot answer (a mode it does not model, or a point outside the downloaded
  area): the caller falls back to the old straight-line estimate and says it is rough.
"""

from __future__ import annotations

import heapq
import math
import re
import threading
from array import array
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

INF = math.inf

# ------------------------------------------------------------------ numbers a person can check
WALK_KMH = 4.8  # an ordinary walking pace, before waiting at lights
SNAP_WALK_FACTOR = 1.25  # from a point to the nearest mapped street: not a straight line either
SNAP_CAR_KMH = 25.0  # a car leaving the mapped main roads for the last stretch on local roads
SNAP_CAR_FACTOR = 1.3
SNAP_MAX_M = {"walk": 500.0, "bike": 500.0, "car": 2000.0}
# minutes added at the end of the trip: parking and walking from the car, locking the bike
OVERHEAD_S = {"walk": 0.0, "bike": 120.0, "car": 300.0, "tram": 0.0}
# Waiting at a node, in seconds, by way of travelling, then by where the traveller is (on a path:
# footway, cycleway, steps ... or on a road), then by the kind of node:
#   "signals"  traffic lights for vehicles (the map marks one per approach and says which way it
#              faces, so a route through a junction waits once, at the lights facing it)
#   "xsignals" a pedestrian crossing with lights (vehicles wait only if somebody presses the button)
#   "xing"     any other marked crossing (zebra, uncontrolled) or a level crossing
# A walker on a road is not crossing it, so he waits for nothing there.
NODE_DELAY_S = {
    "walk": {"path": {"xsignals": 20.0, "xing": 6.0}, "road": {}},
    "bike": {"path": {"xsignals": 15.0, "xing": 3.0},
             "road": {"signals": 15.0, "xsignals": 4.0, "xing": 1.0}},
    "car": {"path": {}, "road": {"signals": 20.0, "xsignals": 4.0, "xing": 1.0}},
}
PATH_HIGHWAYS = {"footway", "path", "pedestrian", "cycleway", "steps", "track", "bridleway",
                 "corridor"}

# surface of the way -> factor on walking / cycling speed
WALK_SURFACE = {
    "sand": 0.6, "mud": 0.5, "grass": 0.8, "ground": 0.85, "dirt": 0.85, "earth": 0.85,
    "unpaved": 0.92, "gravel": 0.92, "fine_gravel": 0.95, "compacted": 0.95,
}
BIKE_SURFACE = {
    "sand": 0.4, "mud": 0.4, "grass": 0.5, "ground": 0.7, "dirt": 0.7, "earth": 0.7,
    "unpaved": 0.75, "gravel": 0.8, "fine_gravel": 0.9, "compacted": 0.9,
    "paving_stones": 0.85, "sett": 0.6, "cobblestone": 0.6, "wood": 0.8,
}

WALK_OK = {
    "footway", "path", "pedestrian", "steps", "track", "residential", "living_street", "service",
    "unclassified", "tertiary", "secondary", "primary", "cycleway", "bridleway", "corridor",
    "road", "tertiary_link", "secondary_link", "primary_link",
}
WALK_ONLY_IF_SIGNED = {"trunk", "trunk_link"}  # no pavement unless the map says walking is fine
BIKE_OK = {
    "cycleway", "path", "track", "residential", "living_street", "service", "unclassified",
    "tertiary", "secondary", "primary", "trunk", "road", "tertiary_link", "secondary_link",
    "primary_link", "trunk_link", "footway", "pedestrian", "steps", "bridleway",
}
BIKE_SPEED_KMH = {
    "cycleway": 17.0, "path": 12.0, "track": 12.0, "residential": 15.0, "living_street": 12.0,
    "service": 12.0, "unclassified": 15.0, "tertiary": 15.0, "secondary": 14.0, "primary": 14.0,
    "trunk": 14.0, "road": 14.0,
}
CAR_OK = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
    "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
    "service", "road",
}
CAR_SPEED_KMH = {  # what a car really averages in town traffic, not the speed limit
    "motorway": 100.0, "motorway_link": 50.0, "trunk": 70.0, "trunk_link": 40.0, "primary": 50.0,
    "primary_link": 35.0, "secondary": 45.0, "secondary_link": 35.0, "tertiary": 40.0,
    "tertiary_link": 30.0, "unclassified": 35.0, "residential": 30.0, "living_street": 10.0,
    "service": 15.0, "road": 30.0,
}
CAR_MAXSPEED_SHARE = 0.8  # a car does not keep to the limit through junctions and traffic
CAR_SKIP_SERVICE = {"driveway", "emergency_access", "drive-through"}

_FOOT_YES = {"yes", "designated", "permissive"}
_NUMBER = re.compile(r"^\s*(\d{1,3})\s*(mph)?\s*$")

M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON_EQUATOR = 111320.0
GRID_DEG = 0.004  # snapping index cell, about 440 m high and 310 m wide
GRID_SAFE_M = GRID_DEG * M_PER_DEG_LON_EQUATOR * 0.69  # the narrower side, in metres
# A point whose nearest street is a little unconnected island (a fenced estate, a car park)
# may use a street of the main network only if one is this close: never across a river.
ISLAND_RESCUE_M = 150.0
MAX_FIELDS = 64  # how many "how long from here" results are kept (about 0.8 MB each)


# ------------------------------------------------------------------ small helpers
def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Metres between two close points (an equirectangular shortcut, exact enough for a city)."""
    dy = (lat2 - lat1) * M_PER_DEG_LAT
    dx = (lon2 - lon1) * M_PER_DEG_LON_EQUATOR * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def _kmh(value: float) -> float:
    return value / 3.6


def snap_seconds(mode: str, metres: float) -> float:
    """Time for the last stretch between a position and the nearest mapped street."""
    if mode == "car":
        return metres * SNAP_CAR_FACTOR / _kmh(SNAP_CAR_KMH)
    return metres * SNAP_WALK_FACTOR / _kmh(WALK_KMH)


@dataclass(frozen=True)
class EdgeRule:
    speed_ms: float
    oneway: int = 0  # 0 both ways, 1 only along the drawn direction, -1 only against it


def _oneway_flag(tags: dict, highway: str, *, bike: bool = False) -> int:
    if bike:
        sides = ("cycleway", "cycleway:left", "cycleway:right")
        cyc = " ".join(str(tags.get(k, "")) for k in sides)
        if (tags.get("oneway:bicycle") == "no" or tags.get("bicycle:oneway") == "no"
                or "opposite" in cyc):
            return 0
    value = tags.get("oneway")
    if value in ("yes", "true", "1"):
        return 1
    if value in ("-1", "reverse"):
        return -1
    if value == "no":
        return 0
    if tags.get("junction") in ("roundabout", "circular"):
        return 1
    if not bike and highway in ("motorway", "motorway_link"):
        return 1
    return 0


def walk_rule(tags: dict) -> EdgeRule | None:
    highway = tags.get("highway")
    foot = tags.get("foot")
    if tags.get("indoor") in ("yes", "room", "area"):
        return None
    if highway in WALK_ONLY_IF_SIGNED:
        if foot not in _FOOT_YES:
            return None
    elif highway not in WALK_OK:
        return None
    if foot in ("no", "private", "use_sidepath"):
        return None
    if tags.get("access") in ("no", "private") and foot not in _FOOT_YES:
        return None
    kmh = 2.4 if highway == "steps" else WALK_KMH
    kmh *= WALK_SURFACE.get(tags.get("surface", ""), 1.0)
    if highway == "track" and "surface" not in tags:
        kmh *= 0.9
    return EdgeRule(_kmh(kmh))


def bike_rule(tags: dict) -> EdgeRule | None:
    highway = tags.get("highway")
    bicycle = tags.get("bicycle")
    if highway not in BIKE_OK or bicycle in ("no", "private", "use_sidepath"):
        return None
    if tags.get("access") in ("no", "private") and bicycle not in _FOOT_YES:
        return None
    if tags.get("indoor") in ("yes", "room", "area"):
        return None
    if highway in ("footway", "pedestrian", "steps", "bridleway"):
        # not marked for bikes: ridden slowly among pedestrians (marked shared paths: faster);
        # steps are carried
        kmh = 2.4 if highway == "steps" else (12.0 if bicycle in _FOOT_YES else 8.0)
    else:
        kmh = BIKE_SPEED_KMH.get(highway, 14.0)
        if tags.get("bicycle") == "designated" or highway == "cycleway":
            kmh = max(kmh, 17.0)
    kmh *= BIKE_SURFACE.get(tags.get("surface", ""), 1.0)
    return EdgeRule(_kmh(kmh), _oneway_flag(tags, highway or "", bike=True))


def car_rule(tags: dict) -> EdgeRule | None:
    highway = tags.get("highway")
    if highway not in CAR_OK:
        return None
    if tags.get("access") in ("no", "private", "permit") or tags.get("motor_vehicle") in (
            "no", "private") or tags.get("vehicle") in ("no", "private"):
        return None
    if highway == "service" and tags.get("service") in CAR_SKIP_SERVICE:
        return None
    kmh = CAR_SPEED_KMH[highway]
    match = _NUMBER.match(str(tags.get("maxspeed", "")))
    if match:
        limit = float(match.group(1)) * (1.609 if match.group(2) else 1.0)
        kmh = min(100.0, max(10.0, limit * CAR_MAXSPEED_SHARE))
    return EdgeRule(_kmh(kmh), _oneway_flag(tags, highway))


RULES = {"walk": walk_rule, "bike": bike_rule, "car": car_rule}


# ------------------------------------------------------------------ the graph
@dataclass
class Graph:
    """One way of travelling: for each node the neighbours and the seconds to get there."""

    adj: list[list[tuple[int, float]]]
    usable: bytearray  # 1 = the node is in the main connected part (safe to snap to)
    grid: dict[tuple[int, int], list[int]]


def dijkstra(adj: list[list[tuple[int, float]]], sources: dict[int, float]) -> list[float]:
    """Shortest seconds from the start nodes (each with its own head start) to every node."""
    dist = [INF] * len(adj)
    heap: list[tuple[float, int]] = []
    for node, start in sources.items():
        if start < dist[node]:
            dist[node] = start
            heap.append((start, node))
    heapq.heapify(heap)
    push, pop = heapq.heappush, heapq.heappop
    while heap:
        d, u = pop(heap)
        if d > dist[u]:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                push(heap, (nd, v))
    return dist


class _Components:
    """Union-find over nodes, to keep only the largest connected part of each graph."""

    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, x: int) -> int:
        parent = self.parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return int(math.floor(lat / GRID_DEG)), int(math.floor(lon / GRID_DEG))


def node_kind(tags: dict) -> tuple[str, str | None] | None:
    """(kind, direction) of a node that makes travellers wait, or None. See NODE_DELAY_S."""
    direction = tags.get("traffic_signals:direction") or tags.get("direction")
    if tags.get("highway") == "traffic_signals":
        return "signals", direction if direction in ("forward", "backward", "both") else None
    if tags.get("crossing") in ("traffic_signals", "traffic_lights"):
        return "xsignals", None
    if tags.get("highway") == "crossing" or tags.get("railway") == "level_crossing":
        return "xing", None
    return None


def _delay(table: dict, node: tuple[str, str | None] | None, forward: bool) -> float:
    """Seconds to wait on arriving at `node`, going along (forward) or against the way's order."""
    if node is None:
        return 0.0
    kind, direction = node
    if kind == "signals" and direction in ("forward", "backward"):
        if (direction == "forward") != forward:
            return 0.0  # the lights face the other way
    return table.get(kind, 0.0)


def _split_runs(nodes: list, geometry: list) -> list[list[tuple[int, float, float]]]:
    """A way as runs of (id, lat, lon); a point outside the downloaded box splits the way."""
    runs: list[list[tuple[int, float, float]]] = []
    current: list[tuple[int, float, float]] = []
    for node_id, point in zip(nodes, geometry, strict=False):
        if not point or "lat" not in point:
            if len(current) > 1:
                runs.append(current)
            current = []
            continue
        current.append((node_id, point["lat"], point["lon"]))
    if len(current) > 1:
        runs.append(current)
    return runs


class StreetMap:
    """The street graphs of one or more downloaded areas, plus the tram, plus a result cache."""

    def __init__(self):
        self.ids: dict[int, int] = {}  # OpenStreetMap node id -> our dense index
        self.lat: list[float] = []
        self.lon: list[float] = []
        self.kinds: dict[int, tuple[str, str | None]] = {}  # node index -> (kind, direction)
        self._node_tags: dict[int, tuple[str, str | None]] = {}
        self.graphs: dict[str, Graph] = {}
        self.tram = None  # TramNetwork, set by from_osm
        self.bus = None  # BusNetwork (routes and stops, no times yet), set by from_osm
        self.way_count = 0
        self._fields: OrderedDict = OrderedDict()
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- building
    @classmethod
    def from_osm(cls, documents: Iterable[dict]) -> StreetMap:
        """Build from Overpass answers: ways (with geometry), signal/crossing nodes, tram routes
        and bus routes with their named stops."""
        from app.geo.bus import BusNetwork  # local imports: both modules import this one
        from app.geo.tram import TramNetwork

        sm = cls()
        ways: dict[int, dict] = {}
        node_tags: dict[int, tuple[str, str | None]] = {}
        relations: dict[int, dict] = {}
        bus_relations: dict[int, dict] = {}
        named: dict[int, tuple[str, float, float]] = {}
        for doc in documents:
            for el in doc.get("elements", []):
                kind = el.get("type")
                tags = el.get("tags") or {}
                if kind == "way" and "highway" in tags and "geometry" in el:
                    ways.setdefault(el["id"], el)
                elif kind == "node":
                    nk = node_kind(tags)
                    if nk and "lat" in el:
                        node_tags[el["id"]] = nk
                    if tags.get("name") and "lat" in el and "lon" in el:
                        named[el["id"]] = (str(tags["name"]), el["lat"], el["lon"])
                elif kind == "relation" and tags.get("route") == "tram":
                    relations.setdefault(el["id"], el)
                elif kind == "relation" and tags.get("route") == "bus":
                    bus_relations.setdefault(el["id"], el)
        sm._build_graphs(ways.values(), node_tags)
        sm.tram = TramNetwork.from_relations(relations.values()) if relations else None
        sm.bus = BusNetwork.from_parts(bus_relations.values(), named) if bus_relations else None
        return sm

    def _index(self, node_id: int, lat: float, lon: float) -> int:
        idx = self.ids.get(node_id)
        if idx is None:
            idx = len(self.lat)
            self.ids[node_id] = idx
            self.lat.append(lat)
            self.lon.append(lon)
            if node_id in self._node_tags:  # known before any street is built: every edge sees it
                self.kinds[idx] = self._node_tags[node_id]
        return idx

    def _build_graphs(self, ways: Iterable[dict],
                      node_tags: dict[int, tuple[str, str | None]]) -> None:
        self._node_tags = node_tags
        edges: dict[str, list[tuple[int, int, float, float, int]]] = {m: [] for m in RULES}
        for way in ways:
            tags = way["tags"]
            rules = {mode: rule(tags) for mode, rule in RULES.items()}
            if not any(rules.values()):
                continue
            self.way_count += 1
            where = "path" if tags.get("highway") in PATH_HIGHWAYS else "road"
            for run in _split_runs(way.get("nodes", []), way["geometry"]):
                idxs = [self._index(i, la, lo) for i, la, lo in run]
                for a, b in zip(idxs, idxs[1:], strict=False):
                    if a == b:
                        continue
                    metres = distance_m(self.lat[a], self.lon[a], self.lat[b], self.lon[b])
                    for mode, rule in rules.items():
                        if rule is None:
                            continue
                        table = NODE_DELAY_S[mode][where]
                        seconds = metres / rule.speed_ms
                        edges[mode].append((
                            a, b,
                            seconds + _delay(table, self.kinds.get(b), True),
                            seconds + _delay(table, self.kinds.get(a), False),
                            rule.oneway,
                        ))
        size = len(self.lat)
        for mode, mode_edges in edges.items():
            self.graphs[mode] = self._make_graph(mode_edges, size)

    def _make_graph(self, edges: list[tuple[int, int, float, float, int]], size: int) -> Graph:
        adj: list[list[tuple[int, float]]] = [[] for _ in range(size)]
        comp = _Components(size)
        for a, b, forward_s, backward_s, oneway in edges:
            comp.union(a, b)
            if oneway >= 0:
                adj[a].append((b, forward_s))
            if oneway <= 0:
                adj[b].append((a, backward_s))
        # only the largest connected part is safe to snap to: a little island of streets (a
        # car park, a fenced estate) must never be taken for "the street next to this job"
        in_graph = {n for a, b, _f, _b, _o in edges for n in (a, b)}
        sizes: dict[int, int] = {}
        for n in in_graph:
            root = comp.find(n)
            sizes[root] = sizes.get(root, 0) + 1
        main = max(sizes, key=sizes.get) if sizes else None
        usable = bytearray(size)
        grid: dict[tuple[int, int], list[int]] = {}
        for n in in_graph:
            if comp.find(n) == main:
                usable[n] = 1
            grid.setdefault(_cell(self.lat[n], self.lon[n]), []).append(n)
        return Graph(adj, usable, grid)

    # ---------------------------------------------------------------- finding a node
    def snap(self, mode: str, lat: float, lon: float) -> tuple[int, float] | None:
        """The street point of this mode nearest to a position, and how many metres away it is.

        The nearest street decides. If it belongs to a little unconnected island, the position
        gets no answer, unless a street of the main network is very close (`ISLAND_RESCUE_M`):
        taking a farther street could mean walking "through" a river or a railway.
        """
        graph = self.graphs.get(mode)
        limit = SNAP_MAX_M.get(mode)
        if graph is None or limit is None or not graph.grid:
            return None
        row, col = _cell(lat, lon)
        nearest: tuple[float, int] | None = None
        nearest_usable: tuple[float, int] | None = None
        ring = 0
        while ring * GRID_SAFE_M <= limit + GRID_SAFE_M:
            for r in range(row - ring, row + ring + 1):
                for c in range(col - ring, col + ring + 1):
                    if max(abs(r - row), abs(c - col)) != ring:
                        continue
                    for n in graph.grid.get((r, c), ()):
                        d = distance_m(lat, lon, self.lat[n], self.lon[n])
                        if nearest is None or d < nearest[0]:
                            nearest = (d, n)
                        if graph.usable[n] and (nearest_usable is None or d < nearest_usable[0]):
                            nearest_usable = (d, n)
            # everything within `ring` cells has been seen: nothing unseen can be nearer than that
            seen = ring * GRID_SAFE_M
            if nearest is not None and nearest[0] <= seen and seen >= ISLAND_RESCUE_M:
                break
            ring += 1
        if nearest is None or nearest[0] > limit:
            return None
        if graph.usable[nearest[1]]:
            return nearest[1], nearest[0]
        if nearest_usable is not None and nearest_usable[0] <= ISLAND_RESCUE_M:
            return nearest_usable[1], nearest_usable[0]
        return None

    # ---------------------------------------------------------------- answering
    def field(self, mode: str, origin: tuple[float, float]) -> Field | None:
        """'How long from `origin` to every point', cached. None = this map cannot answer."""
        key = (mode, round(origin[0], 5), round(origin[1], 5))
        with self._lock:
            hit = self._fields.get(key)
            if hit is not None:
                self._fields.move_to_end(key)
                return hit
            built = self._compute_field(mode, origin)
            if built is not None:
                self._fields[key] = built
                while len(self._fields) > MAX_FIELDS:
                    self._fields.popitem(last=False)
            return built

    def _compute_field(self, mode: str, origin: tuple[float, float]) -> Field | None:
        if mode == "tram":
            if self.tram is None:
                return None
            return self.tram.compute_field(self, origin)
        graph = self.graphs.get(mode)
        if graph is None:
            return None
        start = self.snap(mode, *origin)
        if start is None:
            return None
        node, metres = start
        head_start = snap_seconds(mode, metres)
        dist = array("d", dijkstra(graph.adj, {node: head_start}))  # compact: 8 bytes a point
        return Field(self, mode, mode, dist, OVERHEAD_S[mode])

    def minutes(self, mode: str, origin: tuple[float, float],
                destination: tuple[float, float]) -> float | None:
        field = self.field(mode, origin)
        if field is None:
            return None
        return field.minutes_to(*destination)

    def reach_points(self, mode: str, origin: tuple[float, float],
                     max_minutes: float) -> list[tuple[float, float]] | None:
        """Positions of every street point that can be reached within `max_minutes`."""
        field = self.field(mode, origin)
        if field is None:
            return None
        limit = max_minutes * 60.0 - field.overhead_s
        return [(self.lat[n], self.lon[n]) for n, d in enumerate(field.dist) if d <= limit]


class Field:
    """Seconds from one origin to every node of one graph, plus what to add at the end."""

    def __init__(self, street_map: StreetMap, mode: str, graph_mode: str, dist: list[float],
                 overhead_s: float):
        self.map = street_map
        self.mode = mode
        self.graph_mode = graph_mode  # the graph the distances are measured on
        self.dist = dist
        self.overhead_s = overhead_s

    def minutes_to(self, lat: float, lon: float) -> float | None:
        target = self.map.snap(self.graph_mode, lat, lon)
        if target is None:
            return None
        node, metres = target
        seconds = self.dist[node]
        if seconds == INF:
            return INF
        seconds += snap_seconds(self.graph_mode, metres) + self.overhead_s
        return seconds / 60.0
