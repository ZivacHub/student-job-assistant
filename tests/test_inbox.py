"""Step 4.1 (logic): which ads the Inbox shows, in what order, and how a row reads."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import inbox as inbox_mod
from app.core.inbox import (
    InboxFilters,
    build_row,
    count_phrase,
    describe_age,
    format_pay,
    is_set_aside,
    load_inbox,
    parse_filters,
    safe_http_url,
    score_band,
)
from app.core.profile import get_or_create_profile
from app.models import Job, Source
from app.models.base import utcnow

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def make_source(db, key="t", name="Test izvor"):
    src = Source(key=key, name=name)
    db.add(src)
    db.flush()
    return src


def make_job(db, src, n, **kw):
    values = dict(source=src, external_id=f"e{n}", url=f"https://example.hr/{n}",
                  title=f"Posao {n}", status="seen", channel="email", score=50,
                  first_seen_at=utcnow() - timedelta(minutes=n))
    values.update(kw)
    job = Job(**values)
    db.add(job)
    db.flush()
    return job


def titles(inbox):
    return [r.title for r in inbox.rows]


def load(db, **kw):
    filters = kw.pop("filters", InboxFilters())
    return load_inbox(db, filters, get_or_create_profile(db), has_places=True, **kw)


# ---------------------------------------------------------------- filters from the address
def test_defaults_when_nothing_is_given():
    f = parse_filters({})
    assert (f.view, f.channel, f.min_score, f.page) == ("otvoreni", "", 0, 1)
    assert f.is_default


@pytest.mark.parametrize("params", [
    {"prikaz": "<script>", "kanal": "fax", "ocjena": "abc", "stranica": "x"},
    {"prikaz": "", "kanal": "", "ocjena": "", "stranica": ""},
    {"ocjena": "²", "stranica": "٣"},  # digits that str.isdigit accepts but int() refuses
    {"ocjena": "1e9", "stranica": "-4"},
])
def test_broken_values_fall_back_to_the_default(params):
    f = parse_filters(params)
    assert f.view == "otvoreni" and f.channel == ""
    assert 0 <= f.min_score <= 100 and f.page >= 1


def test_good_values_are_kept_and_clamped():
    f = parse_filters({"prikaz": "sve", "kanal": "phone", "ocjena": "250", "stranica": "3"})
    assert (f.view, f.channel, f.min_score, f.page) == ("sve", "phone", 100, 3)
    assert not f.is_default


def test_query_keeps_only_what_differs_from_the_default():
    f = InboxFilters()
    assert f.query() == ""
    assert f.query(view="sve") == "?prikaz=sve"
    assert f.query(view="sve", channel="email", min_score=60, page=2) == (
        "?prikaz=sve&kanal=email&ocjena=60&stranica=2")
    assert InboxFilters(view="nove", page=4).query(page=1) == "?prikaz=nove"


@pytest.mark.parametrize("n, text", [
    (0, "0 oglasa"), (1, "1 oglas"), (2, "2 oglasa"), (5, "5 oglasa"), (11, "11 oglasa"),
    (21, "21 oglas"), (101, "101 oglas"), (111, "111 oglasa"),
])
def test_count_phrase(n, text):
    assert count_phrase(n) == text


# ---------------------------------------------------------------- small helpers
@pytest.mark.parametrize("minutes, text", [
    (0, "upravo sada"), (1, "prije 1 min"), (59, "prije 59 min"),
    (60, "prije 1 sat"), (120, "prije 2 sata"), (5 * 60, "prije 5 sati"),
    (11 * 60, "prije 11 sati"), (12 * 60, "prije 12 sati"), (21 * 60, "prije 21 sat"),
    (22 * 60, "prije 22 sata"), (24 * 60, "prije 1 dan"), (2 * 24 * 60, "prije 2 dana"),
    (11 * 24 * 60, "prije 11 dana"), (21 * 24 * 60, "prije 21 dan"),
])
def test_age_in_croatian(minutes, text):
    assert describe_age(NOW - timedelta(minutes=minutes), NOW) == text


def test_age_handles_missing_naive_and_future_times():
    assert describe_age(None, NOW) is None
    assert describe_age(NOW.replace(tzinfo=None) - timedelta(minutes=5), NOW) == "prije 5 min"
    assert describe_age(NOW + timedelta(minutes=30), NOW) == "upravo sada"


@pytest.mark.parametrize("url, ok", [
    ("https://www.studentski-poslovi.hr/oglas/1", True),
    ("  http://example.hr/x  ", True),
    ("javascript:alert(1)", False),
    ("JavaScript:alert(1)", False),
    ("data:text/html,<script>alert(1)</script>", False),
    ("ftp://example.hr/x", False),
    ("//example.hr/x", False),
    ("/oglas/1", False),
    ("https://", False),
    ("", False),
    (None, False),
])
def test_only_http_links_are_taken_from_an_ad(url, ok):
    assert (safe_http_url(url) is not None) is ok


def test_pay_text(db):
    src = make_source(db)
    assert format_pay(make_job(db, src, 1, pay_eur_hour=7.5)) == "7,50 €/sat"
    assert format_pay(make_job(db, src, 2, pay_text="po dogovoru")) == "po dogovoru"
    assert format_pay(make_job(db, src, 3, pay_text="x" * 200)) == "x" * 40
    assert format_pay(make_job(db, src, 4)) is None


# ---------------------------------------------------------------- one row
def test_row_shows_minutes_mode_and_place(db):
    src = make_source(db)
    job = make_job(db, src, 1, title="  Konobar/ica \n vikendom ", employer=" Primjer  d.o.o. ",
                   score_reasons=[{"k": "travel", "pts": 20, "text": "x",
                                   "data": {"minutes": 12, "mode": "bus", "place": "Dom"}}])
    row = build_row(job, get_or_create_profile(db), NOW)
    assert row.title == "Konobar/ica vikendom" and row.employer == "Primjer d.o.o."
    assert (row.travel_minutes, row.travel_how) == (12, "autobusom od: Dom")
    assert not row.travel_unknown
    assert row.channel_label == "E-mail" and row.source == "Test izvor"


def test_row_says_when_travel_was_not_checked(db):
    src = make_source(db)
    job = make_job(db, src, 1, score_reasons=[
        {"k": "travel", "pts": 12, "text": "x", "data": {"unknown": True}}])
    row = build_row(job, get_or_create_profile(db), NOW)
    assert row.travel_unknown and row.travel_minutes is None


def test_row_copes_with_odd_data(db):
    src = make_source(db)
    job = make_job(db, src, 1, title="", channel="weird", score=None, score_reasons=None, url="x")
    row = build_row(job, get_or_create_profile(db), NOW)
    assert row.title == "(bez naslova)"
    assert (row.channel, row.channel_label) == ("none", "Bez kontakta")
    assert row.band == "none" and row.url is None and row.travel_minutes is None


@pytest.mark.parametrize("kind, reason", [
    ("category", "vrsta posla je na popisu koji ne želite"),
    ("pay", "plaća je ispod vašeg minimuma"),
    ("travel", "predaleko od svih vaših mjesta"),
])
def test_set_aside_ads_say_why(db, kind, reason):
    src = make_source(db)
    job = make_job(db, src, 1, status="ignored", score=0,
                   score_reasons=[{"k": kind, "pts": 0, "text": "English text", "data": {}}])
    row = build_row(job, get_or_create_profile(db), NOW)
    assert is_set_aside(job) and row.set_aside == reason and row.band == "off"


def test_ad_ignored_by_ivan_is_not_called_set_aside(db):
    src = make_source(db)
    job = make_job(db, src, 1, status="ignored", score=72)
    assert not is_set_aside(job)
    assert build_row(job, get_or_create_profile(db), NOW).status_label == "Zanemareno"


def test_score_band_follows_the_alert_thresholds(db):
    src = make_source(db)
    profile = get_or_create_profile(db)  # 60 and 40 by default
    bands = [score_band(make_job(db, src, i, score=s), profile.notify_threshold,
                        profile.digest_threshold) for i, s in enumerate((100, 60, 59, 40, 39, 1))]
    assert bands == ["high", "high", "mid", "mid", "low", "low"]


# ---------------------------------------------------------------- what is listed, in what order
def test_best_score_first_then_newest_and_unscored_last(db):
    src = make_source(db)
    make_job(db, src, 1, title="B 70 older", score=70, first_seen_at=NOW - timedelta(hours=2))
    make_job(db, src, 2, title="A 90", score=90)
    make_job(db, src, 3, title="B 70 newer", score=70, first_seen_at=NOW - timedelta(hours=1))
    make_job(db, src, 4, title="C none", score=None)
    make_job(db, src, 5, title="D 10", score=10)
    assert titles(load(db)) == ["A 90", "B 70 newer", "B 70 older", "D 10", "C none"]


def test_open_view_hides_what_is_done_with(db):
    src = make_source(db)
    make_job(db, src, 1, title="new", status="new")
    make_job(db, src, 2, title="seen", status="seen")
    for i, status in enumerate(("ignored", "snoozed", "applied"), start=3):
        make_job(db, src, i, title=status, status=status)
    assert sorted(titles(load(db))) == ["new", "seen"]
    assert titles(load(db, filters=InboxFilters(view="nove"))) == ["new"]
    assert len(load(db, filters=InboxFilters(view="sve")).rows) == 5


def test_duplicates_are_never_listed(db):
    src = make_source(db)
    first = make_job(db, src, 1, title="original")
    make_job(db, src, 2, title="copy", duplicate_of=first.id)
    for view in ("otvoreni", "nove", "sve"):
        assert "copy" not in titles(load(db, filters=InboxFilters(view=view)))
    assert load(db, filters=InboxFilters(view="sve")).total == 1


def test_channel_and_minimum_score_filters(db):
    src = make_source(db)
    make_job(db, src, 1, title="mail 80", channel="email", score=80)
    make_job(db, src, 2, title="mail 30", channel="email", score=30)
    make_job(db, src, 3, title="phone 90", channel="phone", score=90)
    make_job(db, src, 4, title="none unscored", channel="none", score=None)
    assert titles(load(db, filters=InboxFilters(channel="email"))) == ["mail 80", "mail 30"]
    assert titles(load(db, filters=InboxFilters(min_score=60))) == ["phone 90", "mail 80"]
    assert titles(load(db, filters=InboxFilters(channel="email", min_score=60))) == ["mail 80"]
    assert load(db, filters=InboxFilters(channel="site")).matching == 0


def test_counts_per_view_and_total(db):
    src = make_source(db)
    make_job(db, src, 1, status="new")
    make_job(db, src, 2, status="seen")
    make_job(db, src, 3, status="seen")
    make_job(db, src, 4, status="ignored", score=0)
    inbox = load(db)
    assert inbox.counts == {"otvoreni": 3, "nove": 1, "sve": 4}
    assert inbox.total == 4 and inbox.matching == 3
    assert inbox.summary == "3 oglasa na koje se još možete javiti"


def test_pages_are_cut_and_a_too_high_page_lands_on_the_last_one(db, monkeypatch):
    monkeypatch.setattr(inbox_mod, "PAGE_SIZE", 2)
    src = make_source(db)
    for i in range(5):
        make_job(db, src, i, title=f"J{i}", score=90 - i)
    p1 = load(db)
    assert (titles(p1), p1.pages, p1.page) == (["J0", "J1"], 3, 1)
    assert titles(load(db, filters=InboxFilters(page=2))) == ["J2", "J3"]
    last = load(db, filters=InboxFilters(page=99))
    assert titles(last) == ["J4"] and last.page == 3 and last.filters.page == 3


def test_empty_database_gives_an_empty_inbox(db):
    inbox = load(db)
    assert inbox.rows == [] and inbox.total == 0 and inbox.pages == 1
