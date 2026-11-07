"""Offline lookup of Croatian towns -> approximate centre coordinates.

Values are town-centre approximations (good to a few km), enough for a first radius filter.
When an ad has a street address, `app.geo.nominatim` refines it. Neighbourhood names that are
not towns map to the town centre (Osijek districts are within ~3 km of the centre).
"""

import unicodedata

# name -> (lat, lon)
CITIES: dict[str, tuple[float, float]] = {
    "osijek": (45.5550, 18.6955),
    "dakovo": (45.3100, 18.4100),
    "valpovo": (45.6619, 18.4172),
    "belisce": (45.6833, 18.4092),
    "beli manastir": (45.7708, 18.6086),
    "darda": (45.6289, 18.6889),
    "cepin": (45.5239, 18.5614),
    "bilje": (45.5806, 18.7517),
    "erdut": (45.5258, 19.0),
    "ernestinovo": (45.4867, 18.6633),
    "nasice": (45.4903, 18.0950),
    "vinkovci": (45.2883, 18.8047),
    "vukovar": (45.3512, 18.9980),
    "zupanja": (45.0789, 18.6958),
    "slavonski brod": (45.1600, 18.0156),
    "pozega": (45.3400, 17.6853),
    "virovitica": (45.8319, 17.3839),
    "slatina": (45.7031, 17.7036),
    "zagreb": (45.8150, 15.9819),
    "rijeka": (45.3271, 14.4422),
    "split": (43.5081, 16.4402),
    "zadar": (44.1194, 15.2314),
    "pula": (44.8666, 13.8496),
    "dubrovnik": (42.6507, 18.0944),
    "varazdin": (46.3057, 16.3366),
    "cakovec": (46.3844, 16.4339),
    "koprivnica": (46.1628, 16.8275),
    "bjelovar": (45.8986, 16.8423),
    "karlovac": (45.4929, 15.5553),
    "sisak": (45.4660, 16.3783),
}

# neighbourhoods / aliases -> town key
ALIASES: dict[str, str] = {
    "tvrdavica": "osijek",
    "tvrda": "osijek",
    "retfala": "osijek",
    "donji grad": "osijek",
    "gornji grad": "osijek",
    "novi grad": "osijek",
    "sjenjak": "osijek",
    "jug 2": "osijek",
    "brijest": "osijek",
    "industrijska cetvrt": "osijek",
    "visnjevac": "osijek",
    "josipovac": "osijek",
    "klisa": "osijek",
    "osijek i okolica": "osijek",
}


def normalize(text: str) -> str:
    """Lowercase, strip diacritics (đ and dj both become d) and extra punctuation/space."""
    text = text.lower().replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = " ".join(text.replace(",", " ").replace(".", " ").replace("-", " ").split())
    return text.replace("dj", "d")  # Đakovo == Djakovo == Dakovo


def lookup_city(name: str | None) -> tuple[float, float] | None:
    """Coordinates for a town/neighbourhood name, or None if unknown."""
    if not name:
        return None
    key = normalize(name)
    if key in CITIES:
        return CITIES[key]
    if key in ALIASES:
        return CITIES[ALIASES[key]]
    # "Osijek, Hrvatska" / "Osijek 31000": try the first known token group
    for known in (*CITIES, *ALIASES):
        if key.startswith(known + " "):
            return CITIES.get(known) or CITIES[ALIASES[known]]
    return None


# nicer spelling for display (keys have no diacritics)
DISPLAY = {
    "dakovo": "Đakovo", "belisce": "Belišće", "cepin": "Čepin", "nasice": "Našice",
    "zupanja": "Županja", "pozega": "Požega", "varazdin": "Varaždin", "cakovec": "Čakovec",
    "beli manastir": "Beli Manastir", "slavonski brod": "Slavonski Brod",
}


def _stem(name: str) -> str:
    """Croatian endings change with the sentence ('u Osijeku', 'iz Osijeka'), so match the stem."""
    return name[:-1] if name[-1] in "aeiou" and len(name) > 4 else name


def find_city_in_text(text: str | None) -> str | None:
    """The town named EARLIEST in free text, as a display name; None if no known town appears.

    Only a guess for pasted ads: the form lets Ivan correct it.
    """
    if not text:
        return None
    norm = f" {normalize(text)}"
    best: tuple[int, str] | None = None
    for name in (*CITIES, *ALIASES):
        pos = norm.find(f" {_stem(name)}")
        if pos != -1 and (best is None or pos < best[0]):
            best = (pos, ALIASES.get(name, name))
    if best is None:
        return None
    key = best[1]
    return DISPLAY.get(key, key.title())
