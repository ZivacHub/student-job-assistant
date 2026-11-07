"""Reading Ivan's reply (plan 5.3b, D42): what the first lines say, and whether Gmail itself
vouches that the mail was really sent by his own account.

Pure functions, no network: the tests feed them whole .eml fixtures.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

TRUSTED_AUTHSERV = "mx.google.com"  # the name Gmail's own servers put in front of their verdict
MIN_NEW_BODY = 20  # shorter text under "DA" is more likely a phone signature than a mail
MAX_TEXT = 20000

YES = "yes"
NO = "no"
UNCLEAR = "unclear"  # the first line is neither DA nor NE
EMPTY = "empty"  # nothing above the quoted part
SHORT = "short"  # DA followed by a few words: neither a plain DA nor a new text

_WROTE = re.compile(r"(wrote|napisao je|napisala je|napisao|napisala)\s*:\s*$", re.IGNORECASE)
_ATTRIBUTION = re.compile(
    r"^(on|na|dana)\s+\S|^(pon|uto|sri|čet|pet|sub|ned|mon|tue|wed|thu|fri|sat|sun)\w*[,.]",
    re.IGNORECASE)
_STOP_LINE = re.compile(r"^(-{2,}\s*original message\s*-{2,}|_{10,}|-- ?)$", re.IGNORECASE)
_PHONE_SIGNATURE = re.compile(
    r"^(sent from my|sent from gmail|sent via|poslano s|poslano sa|get outlook for|"
    r"preuzmite outlook)", re.IGNORECASE)
_COMMAND = re.compile(r"(da|ne)\W*", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    kind: str  # YES, NO, UNCLEAR, EMPTY or SHORT
    new_body: str | None = None  # the text under DA that replaces the draft's body


def above_the_quote(text: str | None) -> list[str]:
    """The lines Ivan typed himself: everything before the quoted original, the attribution line
    ("On Mon ... wrote:" / "... napisao je:"), a signature separator or a phone signature."""
    lines = (text or "")[:MAX_TEXT].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if (stripped.startswith(">") or _STOP_LINE.match(stripped)
                or _PHONE_SIGNATURE.match(stripped)):
            break
        if _ATTRIBUTION.match(stripped) and (
                _WROTE.search(stripped)
                or (index + 1 < len(lines) and _WROTE.search(lines[index + 1].strip()))):
            break
        if _WROTE.search(stripped) and "@" in stripped:
            break
        kept.append(line.rstrip())
    while kept and not kept[-1].strip():
        kept.pop()
    while kept and not kept[0].strip():
        kept.pop(0)
    return kept


def read_command(text: str | None) -> Command:
    """DA, NE, or something the app does not guess about. Only the very first line counts: it must
    be just DA or NE (any case, trailing punctuation allowed). Under DA, a text of at least
    `MIN_NEW_BODY` characters is the new body of the application; a shorter one is refused."""
    lines = above_the_quote(text)
    if not lines:
        return Command(EMPTY)
    match = _COMMAND.fullmatch(lines[0].strip())
    if match is None:
        return Command(UNCLEAR)
    if match.group(1).lower() == "ne":
        return Command(NO)
    rest = "\n".join(lines[1:]).strip()
    if not rest:
        return Command(YES)
    if len(rest) < MIN_NEW_BODY:
        return Command(SHORT)
    return Command(YES, new_body=rest)


# ---------------------------------------------------------------- Gmail's own verdict
_RESULT = re.compile(r"\s*(dkim|spf|dmarc)\s*=\s*([a-z]+)\b(.*)", re.IGNORECASE | re.DOTALL)
_DKIM_DOMAIN = re.compile(r"header\.(?:d|i)\s*=\s*\"?(?:[^@\s;\"]*@)?([^\s;\"()]+)", re.IGNORECASE)


def verdict_summary(values: Sequence[str]) -> str:
    """The topmost verdict reduced to server and results, e.g. `mx.google.com: dkim=pass
    dmarc=pass`, for the log. No header says "nema". Holds nothing of the mail itself."""
    if not values:
        return "nema"
    server, _, verdicts = " ".join(values[0].split()).partition(";")
    results = [f"{m.group(1).lower()}={m.group(2).lower()}"
               for part in verdicts.split(";") if (m := _RESULT.match(part))]
    return f"{server.strip()[:40]}: {' '.join(results) or 'bez rezultata'}"


def domain_of(address: str) -> str:
    return address.rpartition("@")[2].strip().lower()


def sender_check_passed(values: Sequence[str], from_address: str) -> bool:
    """True when the topmost `Authentication-Results` header (written by Gmail's own server, the
    first one on the way in) says the mail really came from the From domain: a DKIM signature of
    that domain checked out, or DMARC passed for it. A header further down is ignored: anyone can
    write a fake one, but not above the one Gmail adds itself. No header, or a header from another
    server, means NO."""
    if not values:
        return False
    top = " ".join(values[0].split())
    server, _, verdicts = top.partition(";")
    if server.strip().lower() != TRUSTED_AUTHSERV:
        return False
    domain = domain_of(from_address)
    if not domain:
        return False
    for part in verdicts.split(";"):
        match = _RESULT.match(part)
        if match is None or match.group(2).lower() != "pass":
            continue
        method, details = match.group(1).lower(), match.group(3)
        if method == "dkim":
            if any(found.lower() == domain for found in _DKIM_DOMAIN.findall(details)):
                return True
        elif method == "dmarc":
            if re.search(rf"header\.from\s*=\s*@?{re.escape(domain)}(?![\w.-])", details,
                         re.IGNORECASE):
                return True
    return False
