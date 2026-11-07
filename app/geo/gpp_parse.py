"""Reads GPP Osijek's timetable pages (plan 4.11, D41). Pure functions, no network.

The two pages (`/polasci-tramvaj/`, `/polasci-autobus/` of web.gpp-osijek.com) are accordions:
every board is a `div.su-spoiler` with a title (tram: "ROUTE: STOP (+/- N min)", bus: "FIRST STOP
- LAST STOP"), a validity heading ("RUJAN 2026."), a link to a PDF and ONE `table.polasci` whose
header is RADNI DAN / SUBOTA / NEDJELJA I BLAGDAN and whose cells are "HH,MM" plus an optional
variant code ("04,55", "13,25 B.B"). A row of text instead of times (a section heading, a legend)
is kept as a note. Dog-transport boards ("PRIJEVOZ PASA ...") are skipped.

Nothing is guessed: a page that does not look like this raises `SourceParseError`, and the caller
keeps the data it had. Page text is untrusted data (it is stored as text, never as markup).
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.sources.http import SourceParseError

BASE_URL = "https://web.gpp-osijek.com"
HEADER = ("RADNI DAN", "SUBOTA", "NEDJELJA I BLAGDAN")
DAY_TYPES = ("weekday", "saturday", "sunday")  # the three columns, in order
MAX_HOUR = 27  # a night service runs past midnight; anything above this is not a time
MAX_NOTES_CHARS = 1000
MAX_CODE_CHARS = 40  # the variant code after a time; the column holds 40
SKIPPED_PREFIX = "PRIJEVOZ PASA"  # dog transport: not a timetable for riding

_TIME = re.compile(r"^(\d{1,2})\s*[,:.]\s*(\d{2})(?!\d)\s*(.*)$")
_TOLERANCE = re.compile(r"\+/-\s*(\d+)\s*min", re.IGNORECASE)
_TOLERANCE_TEXT = re.compile(r"\s*\+/-\s*\d+\s*min\.?", re.IGNORECASE)
_VALID_LABEL = re.compile(r"^[A-ZČĆŽŠĐ ]{3,20}\s*\d{4}\.?$")


@dataclass(frozen=True)
class Departure:
    day_type: str  # weekday / saturday / sunday
    minute: int  # minutes after midnight
    suffix: str | None  # the page's variant code, as written


@dataclass
class Board:
    mode: str
    title: str
    route: str
    stop: str | None
    tolerance_min: int | None
    pdf_url: str | None
    valid_label: str | None
    notes: str | None
    departures: list[Departure] = field(default_factory=list)


@dataclass
class Timetable:
    mode: str
    valid_label: str | None
    boards: list[Board]
    skipped: list[str]  # titles of boards that were left out on purpose

    @property
    def departures(self) -> int:
        return sum(len(board.departures) for board in self.boards)


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def _clean_title(title: str) -> str:
    """The title without the tolerance ("+/- 4 min") and without parentheses left empty by it."""
    title = _TOLERANCE_TEXT.sub("", title)
    title = re.sub(r"\(\s*\)", "", title)
    return " ".join(title.split())


def _valid_label(spoiler) -> str | None:
    for heading in spoiler.find_all("h5"):
        label = " ".join(_text(heading).replace("_", " ").split())
        if _VALID_LABEL.match(label.upper()):
            return label
    return None


def _board(spoiler, mode: str) -> Board | None:
    title_node = spoiler.select_one(".su-spoiler-title")
    if title_node is None or not _text(title_node):
        raise SourceParseError("a timetable board has no title")
    title = _text(title_node)
    if title.upper().startswith(SKIPPED_PREFIX):
        return None
    tables = spoiler.select("table.polasci")
    if len(tables) != 1:
        raise SourceParseError(f"board {title!r} has {len(tables)} tables, expected 1")
    table = tables[0]
    header = tuple(_text(cell).upper() for cell in table.select("thead td, thead th"))
    if header != HEADER:
        raise SourceParseError(f"board {title!r} has an unexpected header {header!r}")

    departures: list[Departure] = []
    notes: list[str] = []
    for row in table.select("tbody tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) != len(DAY_TYPES):
            raise SourceParseError(f"board {title!r} has a row of {len(cells)} cells, expected 3")
        for day_type, cell in zip(DAY_TYPES, cells, strict=True):
            text = _text(cell)
            if not text:
                continue
            match = _TIME.match(text)
            if match is None:
                if text not in notes:
                    notes.append(text)
                continue
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour > MAX_HOUR or minute > 59:
                raise SourceParseError(f"board {title!r} has an impossible time {text!r}")
            code = match.group(3).strip()[:MAX_CODE_CHARS] or None  # a page cannot flood a column
            departures.append(Departure(day_type, hour * 60 + minute, code))
    if not departures:
        raise SourceParseError(f"board {title!r} has no times")

    cleaned = _clean_title(title)
    route, stop = cleaned, None
    if mode == "tram" and ":" in cleaned:  # "ROUTE: STOP"
        route, _, stop = (part.strip() for part in cleaned.partition(":"))
    link = next((a["href"] for a in spoiler.find_all("a", href=True)
                 if ".pdf" in a["href"].lower()), None)
    tolerance = _TOLERANCE.search(title)
    return Board(
        mode=mode, title=title[:300], route=route[:300], stop=stop[:200] if stop else None,
        tolerance_min=int(tolerance.group(1)) if tolerance else None,
        pdf_url=urljoin(BASE_URL, link)[:500] if link else None, valid_label=_valid_label(spoiler),
        notes="\n".join(notes)[:MAX_NOTES_CHARS] or None, departures=departures)


def parse_timetable(html: str, mode: str) -> Timetable:
    """All boards of one page. Raises `SourceParseError` for a page that is not as described
    above (a site redesign, an error page): the caller then keeps what it has."""
    if mode not in ("tram", "bus"):
        raise ValueError(f"unknown mode {mode!r}")
    soup = BeautifulSoup(html or "", "lxml")
    spoilers = soup.select("div.su-spoiler")
    if not spoilers:
        raise SourceParseError("no timetable boards found on the page")
    boards: list[Board] = []
    skipped: list[str] = []
    for spoiler in spoilers:
        board = _board(spoiler, mode)
        if board is None:
            skipped.append(_text(spoiler.select_one(".su-spoiler-title")))
        else:
            boards.append(board)
    if not boards:
        raise SourceParseError("the page holds no timetable boards that can be used")
    labels = Counter(board.valid_label for board in boards if board.valid_label)
    return Timetable(mode=mode, valid_label=labels.most_common(1)[0][0] if labels else None,
                     boards=boards, skipped=skipped)


def content_hash(timetable: Timetable) -> str:
    """A fingerprint of what the page says (not of its markup), to see whether anything changed."""
    payload = [
        [b.title, b.route, b.stop, b.tolerance_min, b.pdf_url, b.valid_label, b.notes,
         sorted((d.day_type, d.minute, d.suffix or "") for d in b.departures)]
        for b in timetable.boards
    ]
    raw = json.dumps([timetable.mode, payload], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
