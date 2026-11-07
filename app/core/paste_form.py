"""Paste-in form (plan 2.7 + 4.4): text copied from Facebook, WhatsApp, Njuškalo or a tip from a
friend becomes an ad.

Two steps, so nothing is saved from a wrong guess: first Ivan pastes the text (and, if he wants,
the link), then he sees what the app understood (title, employer, town, pay, how to apply) and can
fix it, then he saves. The link is only stored: the app never opens it (Facebook and Njuškalo
must not be scraped). Pasted text is untrusted data: it is only read, never obeyed.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.core.inbox import CHANNEL_LABELS, safe_http_url
from app.extract.contacts import choose_channel, extract_contacts
from app.notify.alerts import phone_display
from app.sources.pastein import MAX_CHARS, parse_pasted_ad

MAX_URL = 1000
MAX_TITLE = 300
MAX_EMPLOYER = 300
MAX_CITY = 120
MAX_PAY = 100.0


@dataclass
class PasteValues:
    text: str = ""
    url: str = ""
    title: str = ""
    employer: str = ""
    city: str = ""
    pay: str = ""  # as typed, e.g. "7,50"


@dataclass
class Preview:
    values: PasteValues
    contacts: list[tuple[str, str]] = field(default_factory=list)  # (kind, value)
    channel: str = "none"
    channel_label: str = ""
    shortened: bool = False  # the text was longer than we keep


def clean_text(raw: str | None) -> str:
    return (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def format_pay(value: float | None) -> str:
    return f"{value:.2f}".replace(".", ",") if value is not None else ""


def parse_pay_field(raw: str) -> tuple[float | None, str | None]:
    """'7,50' -> 7.5. Empty -> (None, None). Returns (value, error)."""
    text = (raw or "").strip().replace(",", ".").replace("€", "").strip()
    if not text:
        return None, None
    try:
        value = float(text)
    except ValueError:
        return None, "Plaća: upišite broj, npr. 7,50."
    if not 0 < value <= MAX_PAY:
        return None, f"Plaća: broj mora biti između 0 i {MAX_PAY:.0f} € na sat."
    return value, None


def _check_url(raw: str) -> tuple[str, str | None]:
    text = (raw or "").strip()
    if not text:
        return "", None
    if len(text) > MAX_URL or safe_http_url(text) is None:
        return "", "Poveznica mora počinjati s http:// ili https:// (ili je ostavite praznu)."
    return text, None


def parse_first_step(form: Mapping) -> tuple[PasteValues, dict[str, str]]:
    errors: dict[str, str] = {}
    text = clean_text(form.get("text"))
    url, url_error = _check_url(form.get("url") or "")
    if not text:
        errors["text"] = "Zalijepite tekst oglasa."
    if url_error:
        errors["url"] = url_error
    return PasteValues(text=text, url=url), errors


def build_preview(values: PasteValues) -> Preview:
    """What the app understood from the text. Pure: nothing is saved or fetched."""
    shortened = len(values.text) > MAX_CHARS
    text = values.text[:MAX_CHARS]
    raw = parse_pasted_ad(text, values.url or None)
    found = extract_contacts(text, own_url=values.url or None)
    has_page = bool(values.url)
    channel = choose_channel(found, has_apply_url=has_page)
    guessed = PasteValues(
        text=text, url=values.url, title=raw.title[:MAX_TITLE], employer=raw.employer or "",
        city=raw.city or "", pay=format_pay(raw.pay_eur_hour),
    )
    return Preview(
        values=guessed,
        contacts=[
            (c.kind, phone_display(c.value) if c.kind == "phone" else c.value)
            for c in sorted(found, key=lambda c: (c.kind, -c.confidence))
        ],
        channel=channel,
        channel_label=CHANNEL_LABELS.get(channel, CHANNEL_LABELS["none"]),
        shortened=shortened,
    )


def parse_second_step(form: Mapping) -> tuple[PasteValues, dict, dict[str, str]]:
    """The reviewed form. Returns (values to show again, overrides for the ad, errors)."""
    values, errors = parse_first_step(form)
    values.title = " ".join((form.get("title") or "").split())
    values.employer = " ".join((form.get("employer") or "").split())
    values.city = " ".join((form.get("city") or "").split())
    values.pay = (form.get("pay") or "").strip()
    if len(values.title) > MAX_TITLE:
        errors["title"] = f"Naslov je predug (najviše {MAX_TITLE} znakova)."
    if len(values.employer) > MAX_EMPLOYER:
        errors["employer"] = f"Poslodavac: najviše {MAX_EMPLOYER} znakova."
    if len(values.city) > MAX_CITY:
        errors["city"] = f"Mjesto: najviše {MAX_CITY} znakova."
    pay, pay_error = parse_pay_field(values.pay)
    if pay_error:
        errors["pay"] = pay_error
    overrides: dict = {}
    if values.title:
        overrides["title"] = values.title
    if values.employer:
        overrides["employer"] = values.employer
    if values.city:
        overrides["city"] = values.city
    if pay is not None:
        overrides["pay_eur_hour"] = pay
    return values, overrides, errors
