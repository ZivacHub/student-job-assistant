"""The application text (plan 5.1): a short, formal Croatian email built only from facts.

Facts come from the profile (name, availability, car, CV) and the ad (title, where it was seen).
Nothing about experience or skills is ever invented: the draft says that Ivan is a student, when he
can work (his own words), whether he has a car (only for jobs where it matters) and that the CV is
attached. Choices of Ivan (D33, 2026-10-19): formal "Vi", masculine forms ("Student sam"), and no
mention of Student servis. A CV in English gets an English draft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# categories where "I have my own car" helps (keys of app.scoring.parse.CATEGORY_HR)
CAR_CATEGORIES = {"dostava", "skladiste", "fizicki"}
MANUAL_SOURCE_NAMES = {"Pasted ads", "Ručno dodani oglasi"}
MAX_SUBJECT = 150
MAX_TITLE = 120
MAX_BODY = 4000
SIGNATURE_PLACEHOLDER = "[[Ime i prezime]]"
_PLACEHOLDER = re.compile(r"\[\[[^\]\n]{1,60}\]\]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class DraftFacts:
    title: str
    employer: str | None = None
    category: str | None = None
    source_name: str | None = None  # where the ad was seen, e.g. studentski-poslovi.hr
    availability: str | None = None  # Ivan's own words, e.g. "petkom i subotom navečer"
    signature: str | None = None  # profile.signature, else the full name
    has_car: bool = False
    cv_attached: bool = True
    cv_language: str = "hr"


@dataclass(frozen=True)
class DraftText:
    subject: str
    body: str
    language: str


def one_line(text: str | None, limit: int | None = None) -> str:
    """Single line, no control characters (a title comes from an ad: it is untrusted).

    Double square brackets are reserved for the places Ivan still has to fill in, so text from an
    ad can never look like one.
    """
    clean = " ".join(_CONTROL.sub(" ", text or "").replace("[[", "[").replace("]]", "]").split())
    if limit and len(clean) > limit:
        # the cut plus the " …" stays within the limit
        clean = clean[: limit - 2].rsplit(" ", 1)[0].rstrip(",.;:–- ") + " …"
    return clean


def has_placeholder(text: str | None) -> bool:
    """True while a [[place to fill in]] is left in the text: such a draft must not be approved."""
    return bool(_PLACEHOLDER.search(text or ""))


def _sentence(text: str) -> str:
    return text.rstrip(" .") + "."


def _hr(f: DraftFacts) -> DraftText:
    title = one_line(f.title, MAX_TITLE)
    seen = f", objavljen na {one_line(f.source_name)}" if _real_source(f.source_name) else ""
    middle = ["Student sam."]
    if f.availability and f.availability.strip():
        middle.append(_sentence("Slobodan sam " + one_line(f.availability, 200)))
    if f.has_car and f.category in CAR_CATEGORIES:
        middle.append("Imam vlastiti automobil.")
    if f.cv_attached:
        middle.append("U privitku šaljem životopis.")
    subject = "Prijava na oglas: " + title
    if f.employer:
        subject += " – " + one_line(f.employer, 60)
    body = "\n\n".join([
        "Poštovani,",
        f"javljam se na Vaš oglas „{title}“{seen}.",
        " ".join(middle),
        "U nadi da ćete me pozvati na razgovor, srdačno Vas pozdravljam.",
        f.signature.strip() if f.signature and f.signature.strip() else SIGNATURE_PLACEHOLDER,
    ])
    return DraftText(one_line(subject, MAX_SUBJECT), body, "hr")


def _en(f: DraftFacts) -> DraftText:
    title = one_line(f.title, MAX_TITLE)
    seen = f" published on {one_line(f.source_name)}" if _real_source(f.source_name) else ""
    middle = ["I am a student."]
    if f.availability and f.availability.strip():
        middle.append(_sentence("My availability: " + one_line(f.availability, 200)))
    if f.has_car and f.category in CAR_CATEGORIES:
        middle.append("I have my own car.")
    if f.cv_attached:
        middle.append("I have attached my CV.")
    subject = "Application: " + title
    if f.employer:
        subject += " – " + one_line(f.employer, 60)
    body = "\n\n".join([
        "Dear Sir or Madam,",
        f"I am writing in response to your advertisement \"{title}\"{seen}.",
        " ".join(middle),
        "I look forward to hearing from you.",
        "Kind regards,\n" + (f.signature.strip() if f.signature and f.signature.strip()
                             else SIGNATURE_PLACEHOLDER),
    ])
    return DraftText(one_line(subject, MAX_SUBJECT), body, "en")


def _real_source(name: str | None) -> bool:
    return bool(name) and name not in MANUAL_SOURCE_NAMES


def build_draft_text(facts: DraftFacts) -> DraftText:
    """The subject and body of the application email, Croatian unless the CV is English."""
    return _en(facts) if facts.cv_language == "en" else _hr(facts)
