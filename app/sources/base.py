"""Common shapes for all sources (scrapers, Gmail alerts, paste-in)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ListingItem:
    """One row from a listing page: enough to decide if we already know it."""

    external_id: str
    url: str
    title: str
    employer: str | None = None
    city: str | None = None
    pay_text: str | None = None
    site_label: str | None = None  # the site's own category marker, if any


@dataclass
class RawJob:
    """A job as a source reports it, before scoring."""

    external_id: str
    url: str
    title: str
    employer: str | None = None
    city: str | None = None  # may hold several towns: "Zagreb, Osijek"
    address: str | None = None
    description: str = ""
    pay_text: str | None = None
    pay_eur_hour: float | None = None
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    site_label: str | None = None
    contact_text: str = ""  # the "how to apply" block, highest-trust place for contacts
    apply_note: str | None = None  # employer's instruction, e.g. "Prijave uputiti na telefon"
    extra: dict = field(default_factory=dict)


class BaseSource(ABC):
    key: str
    name: str
    interval_minutes: int = 10
    own_domains: tuple[str, ...] = ()  # emails at these domains are the site's, not the employer's
    backlog_pages: int = 3  # listing pages to read on the very first run (the existing ads)

    def close(self) -> None:
        """Release network clients. Called once when the app stops."""
        return None

    @abstractmethod
    def list_jobs(self, max_pages: int = 1) -> list[ListingItem]:
        """Newest-first listing. Cheap: no per-job requests."""

    @abstractmethod
    def fetch_job(self, item: ListingItem) -> RawJob:
        """Full details for one listing row (one extra request)."""
