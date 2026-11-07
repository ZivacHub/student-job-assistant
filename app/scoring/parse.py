"""Turn messy ad text into structured fields: job category and hourly pay."""

import re

from app.geo.cities import normalize

# Ordered: first matching category wins, so specific ones come before generic ones.
# Keywords are word-start stems matched against normalized text (no diacritics, đ/dj -> d).
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("call_centar", ("call cent", "telesales", "kontakt cent", "videoidentifikacij", "telefonsk")),
    ("it", ("developer", "programer", "software", "web dizajn", "it podrsk", "informatic")),
    ("instrukcije", ("instrukcij", "tutor", "poducavanj")),
    ("djeca", ("animator", "dadilj", "cuvanje djece")),
    ("dostava", ("dostav", "kurir", "wolt", "glovo", "bolt food")),
    ("ugostiteljstvo", ("konobar", "barmen", "sank", "kuhar", "kuhinj", "ugostitelj", "kafic",
                        "restoran", "pizza", "barista")),
    ("skladiste", ("skladist", "utovar", "istovar", "vilicar", "magacin", "pakiranj", "komision")),
    ("promocije", ("promotor", "promocij", "anketar", "letak", "letci", "in store", "degustac",
                   "hostes")),
    ("trgovina", ("prodavac", "blagajn", "trgovin", "kasa", "slaganje robe", "prodaja")),
    ("ciscenje", ("cisc", "spremac", "higijen")),
    ("administracija", ("administrativ", "unos podataka", "asistent", "ured")),
    ("fizicki", ("fizicki", "gradevin", "pomocni radnik", "berba", "sezonsk")),
]

CATEGORY_HR = {
    "ugostiteljstvo": "ugostiteljstvo", "skladiste": "skladište", "call_centar": "pozivni centar",
    "promocije": "promocije", "trgovina": "trgovina", "ciscenje": "čišćenje", "it": "IT",
    "instrukcije": "instrukcije", "djeca": "rad s djecom", "dostava": "dostava",
    "administracija": "administracija", "fizicki": "fizički poslovi",
}


def detect_category(text: str | None) -> str | None:
    """Best-guess category for a title/description, or None."""
    if not text:
        return None
    norm = f" {normalize(text)} "
    for category, keywords in CATEGORY_KEYWORDS:
        for kw in keywords:
            if f" {kw}" in norm:  # keyword = start of a word (stems cover Croatian endings)
                return category
    return None


_PAY_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:[-–]\s*(\d{1,3}(?:[.,]\d{1,2})?))?\s*(?:€|eur\b|eura\b|euro\b)",
    re.IGNORECASE,
)
_NOT_HOURLY = re.compile(r"dnevn|mjese[cč]n|/\s*dan|po danu|godi[sš]nj|neto plac", re.IGNORECASE)
_HOURLY = re.compile(r"/\s*h\b|po satu|satnic|na sat|\bsat\b|€\s*/\s*sat", re.IGNORECASE)


def parse_pay(text: str | None) -> float | None:
    """Hourly pay in EUR (the LOW end of a range, to stay conservative), else None.

    "7.00 - 8.00 €" -> 7.0 ; "7,50 €/h" -> 7.5 ; "50 € dnevno" -> None
    """
    if not text:
        return None
    m = _PAY_RE.search(text)
    if not m:
        return None
    if _NOT_HOURLY.search(text) and not _HOURLY.search(text):
        return None
    value = float(m.group(1).replace(",", "."))
    return value if 3.0 <= value <= 40.0 else None
