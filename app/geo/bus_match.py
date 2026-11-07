"""Joins GPP's bus timetable boards to the map's bus routes (plan step 4.12, D41).

GPP's page names a bus board by its end stops ("IVANOVAC - OSIJEK (GAJEV TRG)"), never by line
number, and the map names stops after streets ("Trg Lj. Gaja"). So the join is by place names, and
it only claims what the names prove:

- a board "A - ... - B" belongs to a map route when a stop of that route is called A and a LATER
  stop is called B (the middle places are not checked);
- a place is written many ways: "OSIJEK (GAJEV TRG)" is also "GAJEV TRG", "SJP ORAO / OPUS ARENA"
  is two places, a stop's platform number ("Ivanovac 1") is dropped, and the route's own name
  ("6: Ivanovac => Osijek (Gajev trg) => Tvrđavica") and its `from` / `to` tags tell where its first
  and last stop are;
- `PLACE_ALIASES` holds the few names that are plainly the same place (checked by hand);
- a board that fits routes of two different lines, a board with two legs, a board with no route at
  all: all of these are listed as NOT matched with the reason, never guessed.

Only the city's own routes take part (`BusRoute.local`): the coaches of other companies do not
appear in GPP's timetable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.geo import mapstore
from app.geo.bus import BusNetwork, BusRoute
from app.geo.cities import normalize
from app.models import Event, TimetableBoard

KIND = "bus_lines_matched"
MAX_LISTED = 60  # how many unmatched boards the Dnevnik entry keeps in its data

# One row = one fact, both sides already in `normalize` form.
PLACE_ALIASES: dict[str, tuple[str, ...]] = {
    "gajev trg": ("trg lj gaja",),  # the main square, Trg Ljudevita Gaja
}

WHY_NO_ROUTE = "no_route"
WHY_SEVERAL_LINES = "several_lines"
WHY_TWO_LEGS = "two_legs"
WHY_ONE_PLACE = "one_place"

# a dash with a space on at least one side separates places; the one in "TENJA 4-3" does not
_DASH = re.compile(r"\s+-\s*|\s*-\s+")
_PARENS = re.compile(r"\(([^()]*)\)")
_PLATFORM_NUMBER = re.compile(r"\s\d+$")


def _names(text: str, *, platform_number: bool = False) -> frozenset[str]:
    """Every way a place can be written, as comparable text (see the module doc)."""
    found: set[str] = set()
    for part in text.split("/"):
        opened = _PARENS.sub(lambda m: " " + m.group(1) + " ", part)
        for candidate in (opened, *_PARENS.findall(part)):
            key = normalize(candidate)
            if not key:
                continue
            found.add(key)
            if platform_number and _PLATFORM_NUMBER.search(key):
                found.add(_PLATFORM_NUMBER.sub("", key))
    for key in tuple(found):
        found.update(PLACE_ALIASES.get(key, ()))
    return frozenset(found)


def board_places(route: str) -> tuple[list[frozenset[str]], str | None]:
    """The places of a GPP route text ("A - B - C") and, when they cannot be used, why not."""
    if "," in route:  # "A-B, B-A": two runs on one board
        return [], WHY_TWO_LEGS
    places = [_names(part) for part in _DASH.split(route) if normalize(part)]
    return (places, None) if len(places) >= 2 else ([], WHY_ONE_PLACE)


def _name_ends(route: BusRoute) -> tuple[frozenset[str], frozenset[str]]:
    """What the route's own name and tags call its first and last place."""
    places = [p for p in route.name.partition(":")[2].split("=>")] if "=>" in route.name else []
    first, last = set(), set()
    if places:
        first |= _names(places[0])
        last |= _names(places[-1])
    if route.origin:
        first |= _names(route.origin)
    if route.destination:
        last |= _names(route.destination)
    return frozenset(first), frozenset(last)


def route_terms(route: BusRoute) -> list[frozenset[str]]:
    """For every stop of the route, the names it answers to."""
    terms = [set(_names(stop.name, platform_number=True)) for stop in route.stops]
    first, last = _name_ends(route)
    terms[0] |= first
    terms[-1] |= last
    return [frozenset(t) for t in terms]


def locate(terms: list[frozenset[str]], places: list[frozenset[str]]) -> tuple[int, int] | None:
    """Stop numbers of the board's first and last place along one route, or None if the route
    does not run from the first place to the last one."""
    first = next((i for i, t in enumerate(terms) if t & places[0]), None)
    last = next((i for i in range(len(terms) - 1, -1, -1) if terms[i] & places[-1]), None)
    if first is None or last is None or last <= first:
        return None
    return first, last


@dataclass(frozen=True)
class BoardMatch:
    board_id: int
    route: str  # GPP's text
    line: str  # the map's line number
    relations: tuple[int, ...]  # the variants of that line that fit the board


@dataclass(frozen=True)
class NotMatched:
    board_id: int
    route: str
    why: str  # one of the WHY_ constants
    lines: tuple[str, ...] = ()  # for WHY_SEVERAL_LINES


@dataclass
class BusMatch:
    boards: int
    matched: list[BoardMatch]
    unmatched: list[NotMatched]
    lines_without_board: list[str]  # the city's lines on the map that no matched board reached

    @property
    def lines(self) -> list[str]:
        return sorted({m.line for m in self.matched}, key=lambda ref: (len(ref), ref))


def match_boards(network: BusNetwork, boards: Iterable[tuple[int, str]]) -> BusMatch:
    """`boards`: (id, GPP route text) of every bus board."""
    local = [(route, route_terms(route)) for route in network.local_routes]
    matched: list[BoardMatch] = []
    unmatched: list[NotMatched] = []
    total = 0
    for board_id, text in boards:
        total += 1
        places, why = board_places(text)
        if why:
            unmatched.append(NotMatched(board_id, text, why))
            continue
        hits = [route for route, terms in local if locate(terms, places)]
        lines = sorted({route.ref for route in hits}, key=lambda ref: (len(ref), ref))
        if not lines:
            unmatched.append(NotMatched(board_id, text, WHY_NO_ROUTE))
        elif len(lines) > 1:
            unmatched.append(NotMatched(board_id, text, WHY_SEVERAL_LINES, tuple(lines)))
        else:
            matched.append(BoardMatch(board_id, text, lines[0],
                                      tuple(sorted(route.relation for route in hits))))
    reached = {m.line for m in matched}
    return BusMatch(total, matched, unmatched,
                    [ref for ref in network.lines if ref not in reached])


# ---------------------------------------------------------------- the Dnevnik entry
def _fingerprint(result: BusMatch) -> str:
    payload = [[(m.route, m.line, m.relations) for m in result.matched],
               [(u.route, u.why, u.lines) for u in result.unmatched], result.lines_without_board]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def event_data(result: BusMatch) -> dict:
    return {
        "boards": result.boards, "matched": len(result.matched), "lines": result.lines,
        "lines_without_board": result.lines_without_board,
        "unmatched": [{"route": u.route, "why": u.why, "lines": list(u.lines)}
                      for u in result.unmatched[:MAX_LISTED]],
        "unmatched_count": len(result.unmatched),
        "why_counts": dict(Counter(u.why for u in result.unmatched)), "fp": _fingerprint(result),
    }


def report_bus_lines(session: Session, streets=None) -> BusMatch | None:
    """Join the loaded bus timetable to the map's bus routes and write what came of it to
    Dnevnik (once: the same result is not written again). Does nothing when there is no map with
    bus routes or no timetable yet. The caller commits."""
    streets = streets if streets is not None else mapstore.get_street_map()
    network = getattr(streets, "bus", None)
    if network is None:
        return None
    boards = session.execute(select(TimetableBoard.id, TimetableBoard.route)
                             .where(TimetableBoard.mode == "bus")
                             .order_by(TimetableBoard.id)).all()
    if not boards:
        return None
    result = match_boards(network, [(b.id, b.route) for b in boards])
    last = session.scalar(select(Event).where(Event.kind == KIND).order_by(Event.id.desc()))
    if last is None or (last.data or {}).get("fp") != _fingerprint(result):
        session.add(Event(kind=KIND, data=event_data(result)))
        session.flush()
    return result
