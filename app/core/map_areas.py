"""The area on the Mjesta map that can really be reached from a place (plan step 4.9, D27).

A place's street field says how long it takes to get to every street point. The points that can be
reached within the limit are drawn as little squares, merged along each row into long strips, so
the shape follows the streets, stops at a river without a bridge and reaches farther along the
tram line. Nothing here is a circle.
"""

from __future__ import annotations

import math

from app.geo.streets import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR, StreetMap

# the ways of travelling the map can show (the bus has no street data)
MAP_MODES = ("walk", "tram", "bike", "car")
CELL_M = {"walk": 120.0, "tram": 150.0, "bike": 200.0, "car": 500.0}  # size of one little square
MAX_RECTANGLES = 1500  # more than this and the squares are made larger, so the page stays light
COARSEN = 1.6


def _merge(points: list[tuple[float, float]], origin: tuple[float, float],
           cell_m: float) -> list[list[float]]:
    lat0, lon0 = origin
    dy = cell_m / M_PER_DEG_LAT
    dx = cell_m / (M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0)))
    rows: dict[int, set[int]] = {}
    for lat, lon in points:
        rows.setdefault(math.floor((lat - lat0) / dy), set()).add(math.floor((lon - lon0) / dx))
    rects: list[list[float]] = []
    for row, columns in rows.items():
        ordered = sorted(columns)
        start = previous = ordered[0]
        for column in ordered[1:] + [None]:
            if column is not None and column == previous + 1:
                previous = column
                continue
            rects.append([round(lat0 + row * dy, 5), round(lon0 + start * dx, 5),
                          round(lat0 + (row + 1) * dy, 5), round(lon0 + (previous + 1) * dx, 5)])
            if column is not None:
                start = previous = column
    return rects


def reach_rectangles(streets: StreetMap, mode: str, origin: tuple[float, float],
                     minutes: float) -> list[list[float]] | None:
    """[[south, west, north, east], ...] of the area reachable within `minutes`, or None when
    the map cannot answer for this place and way of travelling."""
    if mode not in MAP_MODES:
        return None
    points = streets.reach_points(mode, origin, minutes)
    if points is None:
        return None
    cell = CELL_M[mode]
    rects = _merge(points, origin, cell)
    while len(rects) > MAX_RECTANGLES and cell < 5000:
        cell *= COARSEN
        rects = _merge(points, origin, cell)
    return rects
