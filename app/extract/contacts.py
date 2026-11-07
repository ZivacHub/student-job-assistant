"""Pull emails, phone numbers and apply links out of an ad's text.

Everything returned comes from the ad itself (`from_ad`), which is the rule for auto-sending:
we only ever email an address that was printed in that ad.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers

# words just before a contact that tell us it is meant for applicants
_APPLY_CONTEXT = re.compile(
    r"([zž]ivotopis|prijav|\bcv\b|javi|po[sš]alj|[sš]alji|kontakt|e-?mail|mail)", re.IGNORECASE
)
_PHONE_CONTEXT = re.compile(r"(nazov|zov|tel|mob|kontakt|javi|poziv|phone|call|viber|whatsapp)",
                            re.IGNORECASE)
_LINK_CONTEXT = re.compile(r"(prijav|apply|aplicir|obrazac|forma|\bform\b)", re.IGNORECASE)
_LINK_PATH = re.compile(r"(apply|prijav|karijer|career|posao|job)", re.IGNORECASE)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_AT = re.compile(r"\s*[\[\(\{]\s*(?:at|et|@|monkey)\s*[\]\)\}]\s*", re.IGNORECASE)
_DOT = re.compile(r"\s*[\[\(\{]\s*(?:dot|točka|tocka)\s*[\]\)\}]\s*", re.IGNORECASE)

_SKIP_LOCALPARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "webmaster", "postmaster",
                    "privacy", "gdpr", "dpo", "abuse", "mailer-daemon"}
_SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


@dataclass(frozen=True)
class FoundContact:
    kind: str  # "email" | "phone" | "link"
    value: str  # email lowercased / phone E.164 / absolute URL
    confidence: float  # 0..1
    from_ad: bool = True


def _deobfuscate(text: str) -> str:
    return _DOT.sub(".", _AT.sub("@", text))


_SENTENCE_BREAK = re.compile(r"(?<=\d)[.,;]\s|\n|;")


def _before(text: str, pos: int, n: int = 70) -> str:
    """Text just before a contact, cut at the previous number/sentence so hints don't leak."""
    chunk = text[max(0, pos - n):pos]
    parts = _SENTENCE_BREAK.split(chunk)
    return parts[-1]


def extract_emails(text: str, ignore_domains: tuple[str, ...] = ()) -> list[FoundContact]:
    text = _deobfuscate(text)
    out: dict[str, FoundContact] = {}
    for m in _EMAIL_RE.finditer(text):
        addr = m.group(0).strip(".,;:").lower()
        local, _, domain = addr.partition("@")
        if addr.endswith(_SKIP_SUFFIXES) or local in _SKIP_LOCALPARTS:
            continue
        if any(domain == d or domain.endswith("." + d) for d in ignore_domains):
            continue
        conf = 0.95 if _APPLY_CONTEXT.search(_before(text, m.start())) else 0.7
        if addr not in out or conf > out[addr].confidence:
            out[addr] = FoundContact("email", addr, conf)
    return sorted(out.values(), key=lambda c: -c.confidence)


def extract_phones(text: str, region: str = "HR") -> list[FoundContact]:
    text = _EMAIL_RE.sub(" ", _deobfuscate(text))  # digits inside emails are not phones
    out: dict[str, FoundContact] = {}
    for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
        e164 = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        conf = 0.95 if _PHONE_CONTEXT.search(_before(text, m.start, 40)) else 0.6
        if e164 not in out or conf > out[e164].confidence:
            out[e164] = FoundContact("phone", e164, conf)
    return sorted(out.values(), key=lambda c: -c.confidence)


_TRACKING = re.compile(r"^(fbclid|gclid|mc_eid|mc_cid|igshid|utm_.*)$", re.IGNORECASE)


def clean_url(url: str) -> str:
    """Drop tracking parameters (fbclid, utm_*, ...) so links are short and don't leak ids."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING.match(k)]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def extract_links(text: str, own_url: str | None = None) -> list[FoundContact]:
    out: dict[str, FoundContact] = {}
    for m in _URL_RE.finditer(text):
        url = clean_url(m.group(0).rstrip(".,;:!?"))
        if own_url and url.rstrip("/") == own_url.rstrip("/"):
            continue
        if _LINK_CONTEXT.search(_before(text, m.start(), 60)):
            conf = 0.9
        elif _LINK_PATH.search(url):
            conf = 0.6
        else:
            continue
        out.setdefault(url, FoundContact("link", url, conf))
    return sorted(out.values(), key=lambda c: -c.confidence)


def extract_contacts(
    text: str | None,
    ignore_domains: tuple[str, ...] = (),
    own_url: str | None = None,
    region: str = "HR",
) -> list[FoundContact]:
    if not text:
        return []
    return [
        *extract_emails(text, ignore_domains),
        *extract_phones(text, region),
        *extract_links(text, own_url),
    ]


_NOTE_PHONE = re.compile(r"telefon|nazov|pozov|poziv|zovi|broj", re.IGNORECASE)
_NOTE_EMAIL = re.compile(r"e-?mail|[zž]ivotopis|cv|poslati|po[sš]alj", re.IGNORECASE)


def choose_channel(
    contacts: list[FoundContact], has_apply_url: bool = False, note: str | None = None
) -> str:
    """email > phone > site > none. `has_apply_url` = the ad page itself is where to apply.

    An employer note that points to the phone ("Prijave uputiti na broj telefona") beats an
    email address that is also listed, unless the note also asks for an email/CV.
    """
    kinds = {c.kind for c in contacts}
    if note and "phone" in kinds and _NOTE_PHONE.search(note) and not _NOTE_EMAIL.search(note):
        return "phone"
    if "email" in kinds:
        return "email"
    if "phone" in kinds:
        return "phone"
    if "link" in kinds or has_apply_url:
        return "site"
    return "none"


def primary(contacts: list[FoundContact], kind: str) -> FoundContact | None:
    return next((c for c in sorted(contacts, key=lambda c: -c.confidence) if c.kind == kind), None)
