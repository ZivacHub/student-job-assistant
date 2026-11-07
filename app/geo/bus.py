"""The bus lines of the street map (plan step 4.12, D41): routes with their stops in order.

OpenStreetMap has every bus line as one or more routes (one per direction and variant), each a list
of members in travel order; the stops are nodes with a name. This module only READS that into plain
objects. It knows no timetable (GPP's times are `gpp_store.py`; `bus_match.py` joins the two) and
makes no travel time yet (step 4.13).

Which routes are the city's own: a route belongs to GPP Osijek when its `operator` or `network`
says so. Long-distance coaches that pass through Osijek (Flixbus and the like) are kept in the list
but marked as not local, because GPP's pages know nothing about them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.geo.cities import normalize

# A new city: the name its bus company is written with in OpenStreetMap, as `normalize` makes it.
LOCAL_OPERATORS = ("gpp osijek",)
_STOP_ROLES = ("stop", "platform")  # also stop_entry_only, platform_exit_only ...
MAX_NAME_CHARS = 120  # a name is data from outside: nothing longer is kept


@dataclass(frozen=True)
class BusStop:
    name: str
    lat: float
    lon: float
    order: int  # position along the route, from 0


@dataclass(frozen=True)
class BusRoute:
    relation: int  # OpenStreetMap relation id: one direction of one variant of a line
    ref: str  # the line number as printed on the bus ("1", "6")
    name: str  # e.g. "6: Ivanovac => Osijek (Gajev trg) => Tvrđavica"
    origin: str | None  # the route's own `from`
    destination: str | None  # the route's own `to`
    local: bool  # run by the city's bus company
    stops: tuple[BusStop, ...]


def _clip(text: object) -> str | None:
    value = " ".join(str(text or "").split())[:MAX_NAME_CHARS]
    return value or None


def is_local(tags: dict) -> bool:
    return any(normalize(str(tags.get(key) or "")) in LOCAL_OPERATORS
               for key in ("operator", "network"))


class BusNetwork:
    def __init__(self, routes: list[BusRoute]):
        self.routes = routes

    @classmethod
    def from_parts(cls, relations: Iterable[dict],
                   nodes: dict[int, tuple[str, float, float]]) -> BusNetwork | None:
        """`relations`: Overpass route=bus relations (members in order). `nodes`: node id ->
        (name, lat, lon) for every named node of the answer. A member that is not a stop with a
        known position is left out; a route without any stop is dropped."""
        routes: list[BusRoute] = []
        for rel in relations:
            tags = rel.get("tags") or {}
            stops: list[BusStop] = []
            for member in rel.get("members", []):
                role = str(member.get("role") or "")
                if member.get("type") != "node" or not role.startswith(_STOP_ROLES):
                    continue
                node = nodes.get(member.get("ref"))
                if node is not None:
                    stops.append(BusStop(node[0], node[1], node[2], len(stops)))
            if not stops:
                continue
            routes.append(BusRoute(
                relation=rel["id"], ref=_clip(tags.get("ref")) or "?",
                name=_clip(tags.get("name")) or "", origin=_clip(tags.get("from")),
                destination=_clip(tags.get("to")), local=is_local(tags), stops=tuple(stops)))
        return cls(routes) if routes else None

    @property
    def local_routes(self) -> list[BusRoute]:
        return [r for r in self.routes if r.local]

    @property
    def lines(self) -> list[str]:
        """The line numbers of the city's own routes."""
        return sorted({r.ref for r in self.local_routes}, key=lambda ref: (len(ref), ref))
