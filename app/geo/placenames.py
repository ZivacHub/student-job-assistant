"""Place names in a job title ("... Surfer, TC Portanova") that can be looked up on the map.

Most ads give only the town, which puts the job at the town centre. When the title names a
shopping centre or similar, that public name plus the town can be searched on OpenStreetMap
(`Geocoder.resolve`), which finds the real spot. Only titles with such a name are handled; nothing
is guessed from the rest of the text.
"""

import re

_WORD = r"[A-ZČĆŽŠĐ][\w'’\-]*"
_NAME = rf"{_WORD}(?:[ \t]+{_WORD}){{0,2}}"

# (pattern, may the captured name be cut short?): "TC Portanova Osijek" may be name + town, but
# "Avenue Mall" is only whole
_PATTERNS = (
    # "TC Portanova", "T.C. Portanova"
    (re.compile(rf"\bT\.?C\.?[ \t]+({_NAME})"), True),
    # "Shopping centar Avenue", "Trgovački centar Dalmatinska"
    (re.compile(rf"\b(?i:shopping[ \t]+(?:centar|center|centre)|trgova[čc]ki[ \t]+centar)[ \t]+"
                rf"({_NAME})"), True),
    # "Avenue Mall", "Mall Osijek"
    (re.compile(rf"\b({_WORD}[ \t]+Mall)\b"), False),
    (re.compile(rf"\bMall[ \t]+({_NAME})"), True),
)
MAX_QUERIES = 3


def place_names(title: str | None) -> tuple[str, ...]:
    """Names to search for, best first (a longer name before its first word), at most 3."""
    if not title:
        return ()
    out: list[str] = []
    for pattern, may_cut in _PATTERNS:
        for match in pattern.finditer(title):
            words = match.group(1).split()
            for n in range(len(words), 0, -1) if may_cut else (len(words),):
                name = " ".join(words[:n])
                if name not in out:
                    out.append(name)
    return tuple(out[:MAX_QUERIES])
