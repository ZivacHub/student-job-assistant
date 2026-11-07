"""A tiny hand-made map in the shape OpenStreetMap's Overpass API answers with.

Positions are given as grid cells (i, j); one cell is about 100 m, so every distance in the
tests can be worked out by hand. The same cell always gets the same node id, which is what
connects two streets (exactly as in the real data: streets meet only at shared points).
"""

LAT0, LON0 = 45.55, 18.69
DY = 0.0009043  # about 100 m north
DX = 0.001284  # about 100 m east at this latitude


def pt(i: float, j: float) -> tuple[float, float]:
    return LAT0 + j * DY, LON0 + i * DX


def node_id(i: int, j: int) -> int:
    return 100_000 + i * 1000 + j


def way(way_id: int, cells: list[tuple[int, int]], **tags) -> dict:
    tags = {"highway": "residential", **tags}
    return {
        "type": "way", "id": way_id, "tags": tags,
        "nodes": [node_id(i, j) for i, j in cells],
        "geometry": [{"lat": pt(i, j)[0], "lon": pt(i, j)[1]} for i, j in cells],
    }


def signal(i: int, j: int, **tags) -> dict:
    lat, lon = pt(i, j)
    return {"type": "node", "id": node_id(i, j), "lat": lat, "lon": lon,
            "tags": tags or {"highway": "traffic_signals"}}


def tram_route(rel_id: int, ref: str, cells: list[tuple[int, int]],
               roles: dict | None = None) -> dict:
    """One direction of tram line `ref`; `roles` maps a position in `cells` to a role (stop)."""
    roles = roles or {}
    members = []
    for n, (i, j) in enumerate(cells):
        lat, lon = pt(i, j)
        members.append({"type": "node", "ref": node_id(i, j), "role": roles.get(n, "stop"),
                        "lat": lat, "lon": lon})
    return {"type": "relation", "id": rel_id, "tags": {"route": "tram", "ref": ref},
            "members": members}


def bus_route(rel_id: int, ref: str, name: str, stops: list[tuple[str, int, int]], *,
              origin: str | None = None, destination: str | None = None,
              operator: str = "GPP Osijek") -> list[dict]:
    """One direction of bus line `ref` as Overpass answers it with `out body`: the relation (its
    members in order, a street piece among them that must be ignored) and the named stop nodes.
    `stops` are (name, i, j) cells; the same cell is the same node."""
    members = [{"type": "way", "ref": 1, "role": ""}]
    nodes = []
    for stop, i, j in stops:
        lat, lon = pt(i, j)
        stop_id = 500_000 + i * 1000 + j
        members.append({"type": "node", "ref": stop_id, "role": "platform"})
        nodes.append({"type": "node", "id": stop_id, "lat": lat, "lon": lon,
                      "tags": {"name": stop, "public_transport": "platform", "bus": "yes"}})
    tags = {"type": "route", "route": "bus", "ref": ref, "name": name, "operator": operator,
            "network": operator}
    if origin:
        tags["from"] = origin
    if destination:
        tags["to"] = destination
    relation = {"type": "relation", "id": rel_id, "tags": tags, "members": members}
    return [relation, *nodes]


def doc(*elements: dict) -> dict:
    return {"elements": list(elements)}


def line(i0: int, j0: int, i1: int, j1: int) -> list[tuple[int, int]]:
    """Cells of a straight street from (i0, j0) to (i1, j1), one cell apart."""
    steps = max(abs(i1 - i0), abs(j1 - j0))
    return [(i0 + (i1 - i0) * k // steps, j0 + (j1 - j0) * k // steps) for k in range(steps + 1)]


def town() -> dict:
    """West bank and east bank of a river (between i=4 and i=6), joined by ONE bridge at j=0.

        j=3   W street ....(2,3)      (8,3).... E street
        j=0   ---- road ---- bridge ---- road ----
              i=0   2   4   5   6   8   10

    A = (2, 3) on the west bank, B = (8, 3) on the east bank: 600 m apart in a straight line,
    1200 m on foot (down 300 m, across 600 m, up 300 m).
    """
    return doc(
        way(1, line(0, 0, 4, 0)),
        way(2, line(4, 0, 6, 0), bridge="yes"),
        way(3, line(6, 0, 10, 0)),
        way(4, line(0, 3, 4, 3)),  # west street
        way(5, line(6, 3, 10, 3)),  # east street
        way(6, line(2, 0, 2, 3)),  # V2: connects the west street to the road
        way(7, line(8, 0, 8, 3)),  # V8: connects the east street to the road
        way(8, line(0, 0, 0, 3)),  # V0: a second way up on the west bank
    )
