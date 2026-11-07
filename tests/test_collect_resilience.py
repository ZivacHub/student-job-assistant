"""A-02..A-04: failing, blocked or changed sources are recorded and never flood alerts."""

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.profile import get_or_create_profile
from app.geo.nominatim import Geocoder
from app.models import Event, Job, Source
from app.sources.base import BaseSource, ListingItem, RawJob
from app.sources.http import (
    PoliteClient,
    RobotsDisallowed,
    SourceBlocked,
    SourceError,
    SourceNotFound,
    SourceParseError,
)
from app.sources.ingest import collect
from app.sources.studentski_poslovi import BASE, COUNTY_PATH, StudentskiPoslovi

TITLES = ["Konobar", "Skladištar", "Dostavljač", "Programer", "Čistač", "Blagajnik"]


def items(n=5):
    return [ListingItem(str(i), f"https://x.test/{i}", f"{TITLES[i]} u firmi {i}")
            for i in range(n)]


class Fake(BaseSource):
    key, name = "fake", "Fake source"

    def __init__(self, listing=None, fail=None, listing_error=None):
        self.listing = items() if listing is None else listing
        self.fail = fail or {}  # external_id -> exception to raise in fetch_job
        self.listing_error = listing_error

    def list_jobs(self, max_pages=1):
        if self.listing_error:
            raise self.listing_error
        return self.listing

    def fetch_job(self, item):
        if item.external_id in self.fail:
            raise self.fail[item.external_id]
        return RawJob(item.external_id, item.url, item.title, f"Firma {item.external_id} d.o.o.",
                      "Osijek", description="Pošaljite životopis na posao@firma.hr")


@pytest.fixture
def env(db):
    geo_http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    return db, get_or_create_profile(db), Geocoder(db, http=geo_http, sleep=lambda s: None)


def source_row(db) -> Source:
    db.expire_all()
    return db.scalar(select(Source).where(Source.key == "fake"))


# ---------- A-02: crash half-way through the first crawl must not turn the backlog into alerts
def test_crash_in_first_crawl_keeps_backlog_silent(env):
    db, profile, geo = env
    collect(db, Fake(fail={"3": SourceError("network blip")}), profile, [], geo)
    src = source_row(db)
    assert src.baselined_at is None  # run was not clean -> still backlog mode

    later = collect(db, Fake(), profile, [], geo)  # retry: ad 3 now works
    assert [j.external_id for j in later] == ["3"]
    assert all(j.alerted_at is not None for j in later)  # NOT an alert
    assert source_row(db).baselined_at is not None  # clean run completes the baseline

    new_ad = ListingItem("99", "https://x.test/99", "Fotograf u studiju 99")
    fresh = collect(db, Fake(listing=[*items(), new_ad]), profile, [], geo)
    assert len(fresh) == 1 and fresh[0].alerted_at is None  # a genuinely new ad is alertable


# ---------- A-03: failures are recorded
def test_partial_failure_is_recorded_and_other_ads_still_stored(env):
    db, profile, geo = env
    created = collect(db, Fake(fail={"1": SourceError("timeout")}), profile, [], geo)
    assert len(created) == 4
    src = source_row(db)
    assert src.last_status == "partial" and "1: timeout" in src.last_error
    assert src.consecutive_failures == 1 and src.last_items == 4
    assert db.scalar(select(Event).where(Event.kind == "source_run"))


def test_ok_run_resets_failure_counter(env):
    db, profile, geo = env
    collect(db, Fake(listing_error=SourceError("down")), profile, [], geo)
    assert source_row(db).consecutive_failures == 1
    collect(db, Fake(), profile, [], geo)
    src = source_row(db)
    assert src.last_status == "ok" and src.consecutive_failures == 0 and src.last_error is None


def test_listing_error_is_recorded_not_raised(env):
    db, profile, geo = env
    assert collect(db, Fake(listing_error=SourceError("dns")), profile, [], geo) == []
    src = source_row(db)
    assert src.last_status == "error" and src.last_error == "dns"


def test_blocked_stops_the_run_and_source_is_disabled_after_three(env):
    db, profile, geo = env
    blocked = Fake(fail={"0": SourceBlocked("403")})
    for n in (1, 2, 3):
        collect(db, blocked, profile, [], geo)
        src = source_row(db)
        assert src.last_status == "blocked" and src.consecutive_failures == n
        assert src.enabled == (n < 3)
    assert db.query(Job).count() == 0  # the run stopped at the first blocked request


def test_robots_disallow_counts_as_blocked(env):
    db, profile, geo = env
    collect(db, Fake(listing_error=RobotsDisallowed("/x")), profile, [], geo)
    assert source_row(db).last_status == "blocked"


def test_removed_ad_is_not_a_failure(env):
    db, profile, geo = env
    created = collect(db, Fake(fail={"2": SourceNotFound("404")}), profile, [], geo)
    assert len(created) == 4
    src = source_row(db)
    assert src.last_status == "ok" and src.consecutive_failures == 0
    assert src.baselined_at is not None


def test_a_bug_on_one_ad_does_not_hide_the_others(env):
    db, profile, geo = env
    created = collect(db, Fake(fail={"1": ValueError("surprise")}), profile, [], geo)
    assert len(created) == 4
    src = source_row(db)
    assert src.last_status == "partial" and "ValueError: surprise" in src.last_error


# ---------- A-04: layout change must not look like a quiet success
ROBOTS = "User-agent: *\nCrawl-delay: 10\nDisallow: /pretraga\n"


def real_source() -> StudentskiPoslovi:
    client = PoliteClient(min_interval_s=10, user_agent="T/1", http=httpx.Client(),
                          sleep=lambda s: None)
    return StudentskiPoslovi(client)


@respx.mock
def test_page_without_cards_raises_parse_error():
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get(f"{BASE}{COUNTY_PATH}").mock(
        return_value=httpx.Response(200, text="<html><body>" + "<p>nova struktura</p>" * 400)
    )
    with pytest.raises(SourceParseError):
        real_source().list_jobs()


@respx.mock
def test_collect_records_parse_error(env):
    db, profile, geo = env
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get(f"{BASE}{COUNTY_PATH}").mock(
        return_value=httpx.Response(200, text="<html><body>" + "<p>nova struktura</p>" * 400)
    )
    assert collect(db, real_source(), profile, [], geo) == []
    src = db.scalar(select(Source).where(Source.key == "studentski_poslovi"))
    assert src.last_status == "parse_error" and "layout" in src.last_error
    assert src.baselined_at is None


@respx.mock
def test_tiny_error_page_is_a_fetch_problem_not_a_layout_change():
    """A short body (maintenance page) must not be reported as 'layout changed'."""
    respx.get(f"{BASE}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get(f"{BASE}{COUNTY_PATH}").mock(return_value=httpx.Response(200, text="<html>x</html>"))
    assert real_source().list_jobs() == []
