from datetime import UTC, datetime, timedelta

import pytest

from app.geo.cities import lookup_city
from app.models import Job, Place, Profile
from app.scoring.engine import score_job
from app.scoring.parse import detect_category, parse_pay

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
OSIJEK = lookup_city("Osijek")
DAKOVO = lookup_city("Đakovo")
ZAGREB = lookup_city("Zagreb")


def job(**kw) -> Job:
    base = dict(title="Konobar", category="ugostiteljstvo", pay_eur_hour=8.0, channel="email",
                lat=OSIJEK[0], lon=OSIJEK[1], posted_at=NOW - timedelta(minutes=10))
    base.update(kw)
    return Job(**base)


def profile(**kw) -> Profile:
    base = dict(has_car=True, categories_wanted=["ugostiteljstvo", "skladiste"],
                categories_excluded=["ciscenje"], min_pay_eur_hour=6.0, weights={})
    base.update(kw)
    return Profile(**base)


def home(**kw) -> Place:
    base = dict(label="Home", lat=OSIJEK[0], lon=OSIJEK[1],
                max_minutes_by_mode={"car": 30, "bus": 45}, bonus_points=0, active=True)
    base.update(kw)
    return Place(**base)


def partner(**kw) -> Place:
    base = dict(label="Partner", lat=DAKOVO[0], lon=DAKOVO[1],
                max_minutes_by_mode={"car": 20}, bonus_points=10, active=True)
    base.update(kw)
    return Place(**base)


def run(j=None, p=None, places=None):
    return score_job(j or job(), p or profile(), places if places is not None else [home()], NOW)


# ---------- hard filters
def test_01_ideal_job_scores_high_with_reasons():
    r = run()
    assert not r.excluded and 90 <= r.score <= 94
    assert {x["k"] for x in r.reasons} >= {"category", "travel", "pay", "freshness", "channel"}


def test_02_excluded_category():
    r = run(job(category="ciscenje"))
    assert r.excluded and r.score == 0 and r.reasons[0]["k"] == "category"


def test_03_pay_below_minimum():
    r = run(job(pay_eur_hour=5.0))
    assert r.excluded and r.reasons[0]["k"] == "pay"


def test_04_far_city_is_excluded_with_distance_reason():
    r = run(job(lat=ZAGREB[0], lon=ZAGREB[1]))
    assert r.excluded and "Too far" in r.reasons[0]["text"]


def test_05_no_car_bus_only_dakovo_is_too_far():
    r = run(job(lat=DAKOVO[0], lon=DAKOVO[1]), profile(has_car=False))
    assert r.excluded


def test_06_dakovo_reachable_with_car_and_generous_limit():
    r = run(job(lat=DAKOVO[0], lon=DAKOVO[1]), places=[home(max_minutes_by_mode={"car": 80})])
    assert not r.excluded


# ---------- bonus place (partner)
def test_07_partner_place_makes_far_job_possible_and_adds_bonus():
    j = job(lat=DAKOVO[0], lon=DAKOVO[1])
    without = run(j, places=[home(), partner(bonus_points=0)])
    with_bonus = run(j, places=[home(), partner()])
    assert not with_bonus.excluded
    assert with_bonus.score == without.score + 10
    assert any(x["text"] == "Near Partner" for x in with_bonus.reasons)


def test_08_bonus_is_capped():
    places = [home(bonus_points=20), partner(bonus_points=20, max_minutes_by_mode={"car": 200})]
    r = run(job(lat=OSIJEK[0], lon=OSIJEK[1]), places=places)
    bonus = sum(x["pts"] for x in r.reasons if x["k"] == "bonus")
    assert bonus == 15


def test_09_inactive_place_is_ignored():
    r = run(job(lat=DAKOVO[0], lon=DAKOVO[1]), places=[home(), partner(active=False)])
    assert r.excluded


# ---------- neutral cases
def test_10_unknown_location_is_not_excluded_but_flagged():
    r = run(job(lat=None, lon=None))
    assert not r.excluded
    assert any("not checked" in x["text"] for x in r.reasons)


def test_11_no_places_configured_is_neutral():
    r = run(places=[])
    assert not r.excluded and any("not checked" in x["text"] for x in r.reasons)


def test_12_unknown_pay_and_category_are_neutral_not_excluded():
    r = run(job(pay_eur_hour=None, category=None))
    assert not r.excluded
    assert 0 < r.score < run().score


# ---------- ordering: newer, better channel, nearer
def test_13_freshness_decays():
    new = run(job(posted_at=NOW - timedelta(minutes=5))).score
    old = run(job(posted_at=NOW - timedelta(hours=30))).score
    assert new - old >= 15


def test_14_channel_ordering_email_phone_site_none():
    s = [run(job(channel=c)).score for c in ("email", "phone", "site", "none")]
    assert s == sorted(s, reverse=True) and len(set(s)) == 4


def test_15_nearer_beats_farther():
    near = run(job(lat=OSIJEK[0], lon=OSIJEK[1])).score
    valpovo = lookup_city("Valpovo")
    far = run(job(lat=valpovo[0], lon=valpovo[1])).score
    assert near > far


def test_16_weights_override_and_cap_100():
    p = profile(weights={"freshness": 0, "channel": 0})
    assert run(p=p).score < run().score
    huge = profile(weights={"category": 100, "travel": 100})
    assert run(p=huge).score == 100


def test_17_naive_datetimes_from_sqlite_are_handled():
    r = run(job(posted_at=datetime(2026, 10, 9, 11, 50)))  # naive = UTC
    assert not r.excluded and r.score > 80


# ---------- parsing
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Konobar/ica u kafiću", "ugostiteljstvo"),
        ("Agent u call centru - Telesales", "call_centar"),
        ("Utovar-istovar guma", "skladiste"),
        ("Pomoć pri postavljanju teniskog balona", None),
        ("In store promocije", "promocije"),
        ("Dostavljač Wolt", "dostava"),
        ("Junior Software Developer", "it"),
        ("Čišćenje ureda", "ciscenje"),
        ("Fizički poslovi", "fizicki"),
        ("Agent/ica za videoidentifikaciju na njemačkom jeziku", "call_centar"),
        ("", None),
    ],
)
def test_detect_category(text, expected):
    assert detect_category(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("7.00 - 8.00 €", 7.0),
        ("8.00 €", 8.0),
        ("7,50 €/h", 7.5),
        ("od 6 eura po satu", 6.0),
        ("50 € dnevno", None),
        ("1200 €", None),
        ("plaća po dogovoru", None),
        (None, None),
    ],
)
def test_parse_pay(text, expected):
    assert parse_pay(text) == expected
