"""Phase 5 (P2): a new good match with an email address in the ad gets a draft while it is
collected; the first (backlog) run, ads without an address and failures do not."""

from datetime import UTC, datetime

import httpx
import pytest

import app.sources.ingest as ingest_module
from app.core.profile import get_or_create_profile
from app.geo.cities import lookup_city
from app.geo.nominatim import Geocoder
from app.models import Application, Place, Source
from app.sources.base import RawJob
from app.sources.ingest import ingest_raw

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(db):
    profile = get_or_create_profile(db)
    profile.full_name, profile.categories_wanted, profile.notify_threshold = (
        "Ivan Primjer", ["ugostiteljstvo"], 30)
    osijek = lookup_city("Osijek")
    home = Place(label="Home", lat=osijek[0], lon=osijek[1], kind="home",
                 max_minutes_by_mode={"car": 30, "bus": 45}, active=True)
    profile.has_car = True
    db.add(home)
    source = Source(key="t", name="studentski-poslovi.hr")
    db.add(source)
    db.commit()
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    return db, profile, [home], Geocoder(db, http=http, sleep=lambda s: None), source


def raw(n=1, *, contact="Javite se na posao@example.hr", title="Konobar/ica vikendom"):
    return RawJob(external_id=f"e{n}", url=f"https://example.hr/oglas/{n}", title=title,
                  employer="Caffe Bar Luna d.o.o.", city="Osijek", pay_eur_hour=8.0,
                  description="Traži se konobar/ica za vikend.", contact_text=contact,
                  posted_at=NOW)


def test_a_new_good_match_with_an_address_gets_a_draft_that_waits_for_approval(env):
    db, profile, places, geo, source = env
    job = ingest_raw(db, source, raw(), profile, places, geo, now=NOW)
    assert job.score >= 30 and job.channel == "email"
    [application] = db.query(Application).all()
    assert application.job_id == job.id and application.state == "queued"
    assert application.to_email == "posao@example.hr"
    assert application.approved_at is None and application.sent_at is None
    assert application.dry_run is True
    assert application.subject.startswith("Prijava na oglas: Konobar/ica vikendom")


def test_the_first_run_of_a_source_prepares_no_drafts_for_the_old_backlog(env):
    db, profile, places, geo, source = env
    ingest_raw(db, source, raw(), profile, places, geo, baseline=True, now=NOW)
    assert db.query(Application).count() == 0


def test_an_ad_without_an_address_in_it_gets_no_draft(env):
    db, profile, places, geo, source = env
    ingest_raw(db, source, raw(contact="Nazovite 091 234 5678"), profile, places, geo, now=NOW)
    assert db.query(Application).count() == 0


def test_an_ad_below_the_alert_line_gets_no_draft(env):
    db, profile, places, geo, source = env
    profile.notify_threshold = 100
    ingest_raw(db, source, raw(), profile, places, geo, now=NOW)
    assert db.query(Application).count() == 0


def test_a_problem_while_preparing_the_draft_never_stops_the_collector(env, monkeypatch):
    db, profile, places, geo, source = env

    def boom(*args, **kwargs):
        raise RuntimeError("template broke")

    monkeypatch.setattr(ingest_module, "ensure_draft", boom)
    job = ingest_raw(db, source, raw(), profile, places, geo, now=NOW)
    assert job is not None and job.score is not None
    assert db.query(Application).count() == 0
