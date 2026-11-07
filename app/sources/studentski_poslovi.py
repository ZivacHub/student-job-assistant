"""studentski-poslovi.hr collector (Osijek-Baranja county).

robots.txt (checked 2026-10-09): Crawl-delay 10 s; /pretraga and /api/ disallowed. We only read
/poslovi/zupanija/<county> (server-rendered listing) and /posao/<slug>-<id> (detail with
schema.org JobPosting JSON-LD plus an "Informacije za prijavu" block holding email/phone/link).
"""

import json
import logging
import re
from datetime import datetime

from bs4 import BeautifulSoup

from app.sources.base import BaseSource, ListingItem, RawJob
from app.sources.http import PoliteClient, SourceParseError

log = logging.getLogger(__name__)

BASE = "https://studentski-poslovi.hr"
COUNTY_PATH = "/poslovi/zupanija/osjecko-baranjska"
MIN_REAL_PAGE_CHARS = 5000  # a real listing page is ~100 KB; tiny pages are errors, not layouts

_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}\.?$")
_ID_RE = re.compile(r"-(\d+)/?$")
_LEADING_SYMBOLS = re.compile(r"^[^\w(]+", re.UNICODE)
_APPLY_END = ("Drugi studentski poslovi", "Želite se prijaviti na oglas?")


def _split_label(raw_title: str) -> tuple[str, str | None]:
    """'☕ Konobar/Šanker' -> ('Konobar/Šanker', '☕')."""
    raw_title = " ".join(raw_title.split())
    m = _LEADING_SYMBOLS.match(raw_title)
    if not m:
        return raw_title, None
    return raw_title[m.end():].strip(), m.group(0).strip() or None


def parse_listing(html: str) -> list[ListingItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[ListingItem] = []
    for card in soup.select("div.job-post"):
        link = card.select_one('a[href*="/posao/"]')
        h5 = card.select_one("h5")
        if not link or not h5:
            continue
        url = link["href"]
        m = _ID_RE.search(url)
        if not m:
            continue
        title, label = _split_label(h5.get_text(" ", strip=True))
        pay = city = None
        employer = None
        for span in card.find_all("span"):
            text = " ".join(span.get_text(" ", strip=True).split())
            icon = span.find("i")
            if icon is not None:
                classes = " ".join(icon.get("class", []))
                if "money" in classes:
                    pay = text
                elif "map-marker" in classes:
                    city = re.sub(r"\s*,\s*", ", ", text)
            elif employer is None and text and not _DATE_RE.match(text) \
                    and not _LEADING_SYMBOLS.match(text) and not text.startswith("Pogledaj"):
                employer = text
        items.append(ListingItem(m.group(1), url, title, employer, city, pay, label))
    return items


def _jobposting(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                return obj
    return {}


def _dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _locations(posting: dict) -> tuple[str | None, str | None]:
    """(towns joined by ', ', first street address)."""
    locs = posting.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    towns, street = [], None
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        town = addr.get("addressLocality")
        if town and town not in towns:
            towns.append(town)
        street = street or addr.get("streetAddress")
    return (", ".join(towns) or None), street


def _apply_block(soup: BeautifulSoup) -> tuple[str, str | None]:
    """Text of 'Informacije za prijavu' (+ external link) and the employer's note."""
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    try:
        start = lines.index("Informacije za prijavu") + 1
    except ValueError:
        return "", None
    end = next((i for i in range(start, len(lines)) if lines[i] in _APPLY_END), len(lines))
    block = lines[start:end]
    note = None
    if "Napomena poslodavca" in block:
        i = block.index("Napomena poslodavca")
        note = " ".join(block[i + 1:]).strip() or None
    text = "\n".join(block)
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == "Idi na poveznicu":
            text += f"\nPrijavite se: {a['href']}"
    return text, note


def parse_detail(html: str, item: ListingItem) -> RawJob:
    soup = BeautifulSoup(html, "lxml")
    posting = _jobposting(soup)
    if not posting:
        log.warning("no JobPosting JSON-LD on %s: date, pay and address will be missing", item.url)
    contact_text, note = _apply_block(soup)

    desc_html = posting.get("description") or ""
    description = BeautifulSoup(desc_html, "lxml").get_text("\n", strip=True) if desc_html else ""

    towns, street = _locations(posting)
    salary = (posting.get("baseSalary") or {}).get("value") or {}
    pay = salary.get("value") if salary.get("unitText") == "HOUR" else None
    org = posting.get("hiringOrganization") or {}

    return RawJob(
        external_id=item.external_id,
        url=posting.get("url") or item.url,
        title=posting.get("title") or item.title,
        employer=org.get("name") or item.employer,
        city=item.city or towns,
        address=street,
        description=description,
        pay_text=item.pay_text,
        pay_eur_hour=float(pay) if isinstance(pay, int | float) else None,
        posted_at=_dt(posting.get("datePosted")),
        expires_at=_dt(posting.get("validThrough")),
        site_label=item.site_label,
        contact_text=contact_text,
        apply_note=note,
    )


class StudentskiPoslovi(BaseSource):
    key = "studentski_poslovi"
    name = "studentski-poslovi.hr (Osijek-Baranja)"
    interval_minutes = 10
    own_domains = ("studentski-poslovi.hr",)

    def __init__(self, client: PoliteClient | None = None):
        self.client = client or PoliteClient(min_interval_s=10.0)

    def close(self) -> None:
        self.client.close()

    def list_jobs(self, max_pages: int = 1) -> list[ListingItem]:
        seen: dict[str, ListingItem] = {}
        for page in range(1, max_pages + 1):
            url = f"{BASE}{COUNTY_PATH}" + (f"?page={page}" if page > 1 else "")
            html = self.client.get(url).text
            items = parse_listing(html)
            if not items:
                if page == 1 and len(html) > MIN_REAL_PAGE_CHARS and "job-post" not in html:
                    # a normal-sized page without a single card: the layout has changed
                    raise SourceParseError(f"no job cards found on {url} (layout changed?)")
                break
            for it in items:
                seen.setdefault(it.external_id, it)
        return list(seen.values())

    def fetch_job(self, item: ListingItem) -> RawJob:
        return parse_detail(self.client.get(item.url).text, item)

