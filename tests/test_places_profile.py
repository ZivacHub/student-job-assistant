"""Step 4.2 (logic): places, profile form, re-scoring. Messages are in Croatian on purpose."""

from datetime import UTC, datetime, timedelta

import pytest
from starlette.datastructures import FormData

from app.core.places import (
    delete_place,
    list_places,
    map_data,
    parse_place_form,
    place_summary,
    save_place,
    town_options,
)
from app.core.profile import get_or_create_profile
from app.core.profile_form import parse_profile_form, save_profile
from app.geo.cities import lookup_city
from app.geo.travel import reach_km, travel_minutes
from app.models import Cv, Job, Place, Source
from app.scoring.rescore import rescore_jobs

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def place_form(**kw):
    base = {"label": "Dom", "kind": "home", "town": "osijek", "min_bus": "40", "bonus": "0",
            "active": "on"}
    base.update(kw)
    return base


# ---------- reach circles
@pytest.mark.parametrize("mode", ["car", "bus", "bike", "walk"])
def test_reach_km_is_the_inverse_of_travel_minutes(mode):
    km = reach_km(mode, 40)
    assert travel_minutes(km, mode) == pytest.approx(40)


def test_reach_is_zero_when_the_time_is_only_the_fixed_overhead():
    assert reach_km("bus", 5) == 0.0


# ---------- place form
def test_town_choice_gives_the_position_of_the_town():
    values, errors = parse_place_form(place_form())
    assert errors == {}
    assert (values.lat, values.lon) == lookup_city("Osijek")
    assert values.minutes == {"bus": 40}


def test_clicked_point_beats_the_town_and_comma_decimals_are_fine():
    values, errors = parse_place_form(place_form(town="", lat="45,5601", lon="18,6921"))
    assert errors == {} and (values.lat, values.lon) == (45.5601, 18.6921)


def test_nothing_chosen_says_what_to_do():
    _, errors = parse_place_form(place_form(town=""))
    assert errors["lat"] == "Odaberite grad ili kliknite na kartu da postavite mjesto."


def test_only_one_coordinate_is_refused():
    _, errors = parse_place_form(place_form(town="", lat="45.5"))
    assert "i širinu i dužinu" in errors["lat"]


def test_point_outside_croatia_hints_at_swapped_numbers():
    _, errors = parse_place_form(place_form(town="", lat="18.69", lon="45.55"))
    assert "nije u Hrvatskoj" in errors["lat"]


def test_non_numeric_coordinates():
    _, errors = parse_place_form(place_form(town="", lat="abc", lon="x"))
    assert "moraju biti brojevi" in errors["lat"]


def test_name_is_required_and_limited():
    assert "Upišite naziv" in parse_place_form(place_form(label="  "))[1]["label"]
    assert "najviše 60" in parse_place_form(place_form(label="x" * 61))[1]["label"]


@pytest.mark.parametrize("raw", ["0", "241", "-5", "dva"])
def test_minutes_must_be_between_1_and_240(raw):
    _, errors = parse_place_form(place_form(min_bus=raw))
    assert "min_bus" in errors


def test_at_least_one_way_of_travelling_is_needed():
    _, errors = parse_place_form(place_form(min_bus=""))
    assert "barem jedan način" in errors["min_car"]


@pytest.mark.parametrize("raw", ["-1", "16", "x"])
def test_bonus_limits(raw):
    assert "bonus" in parse_place_form(place_form(bonus=raw))[1]


def test_checkbox_unchecked_means_inactive_and_unknown_kind_becomes_other():
    values, _ = parse_place_form(place_form(kind="evil", active=""))
    assert values.active is False and values.kind == "other"


# ---------- saving
def test_save_edit_and_delete(db):
    values, _ = parse_place_form(place_form())
    place = save_place(db, values)
    db.commit()
    assert place.address == "Osijek" and place.max_minutes_by_mode == {"bus": 40}
    values.label, values.bonus = "Dom 2", 5
    save_place(db, values, place.id)
    db.commit()
    assert [(p.label, p.bonus_points) for p in list_places(db)] == [("Dom 2", 5)]
    assert delete_place(db, place.id) is True and list_places(db) == []
    assert delete_place(db, 999) is False


def test_editing_a_place_that_is_gone_raises_a_plain_error(db):
    values, _ = parse_place_form(place_form())
    with pytest.raises(LookupError, match="ne postoji"):
        save_place(db, values, 999)


def test_clicked_point_has_no_town_label(db):
    values, _ = parse_place_form(place_form(town="", lat="45.56", lon="18.69"))
    assert save_place(db, values).address is None


def test_summary_and_map_data_ignore_car_when_there_is_no_car(db):
    values, _ = parse_place_form(place_form(min_car="60", min_walk="25", bonus="10"))
    place = save_place(db, values)
    text = place_summary(place, has_car=False)
    assert "autobusom 40 min" in text and "pješice 25 min" in text and "+10 bodova" in text
    assert "autom 60 min" not in text and "nemate auto" in text
    no_car, with_car = map_data([place], False)[0], map_data([place], True)[0]
    assert with_car["radius_m"] > no_car["radius_m"] > 0  # car would reach farther than the bus
    assert no_car["radius_m"] == round(reach_km("bus", 40) * 1000)


def test_town_list_is_sorted_and_has_croatian_spelling():
    names = [t["name"] for t in town_options()]
    assert names == sorted(names, key=str.lower)
    assert "Đakovo" in names and "Osijek" in names


# ---------- profile form
def profile_form(**kw):
    items = {"full_name": "Ivan Primjer", "tone": "Vi", "min_pay": "6,5",
             "notify_threshold": "60", "digest_threshold": "40", "digest_interval_hours": "3"}
    items.update(kw)
    pairs = []
    for key, value in items.items():
        for v in (value if isinstance(value, list) else [value]):
            pairs.append((key, v))
    return FormData(pairs)


def test_valid_profile_form():
    form = profile_form(wanted=["ugostiteljstvo", "skladiste"], excluded=["ciscenje"],
                        has_car="on")
    values, errors = parse_profile_form(form, [])
    assert errors == {}
    assert values.wanted == ["ugostiteljstvo", "skladiste"] and values.has_car
    assert values.min_pay == 6.5


def test_unknown_categories_are_dropped():
    values, _ = parse_profile_form(profile_form(wanted=["ugostiteljstvo", "hakiranje"]), [])
    assert values.wanted == ["ugostiteljstvo"]


def test_same_category_wanted_and_unwanted_is_refused():
    _, errors = parse_profile_form(profile_form(wanted=["it"], excluded=["it"]), [])
    assert "ne može biti i željena i neželjena" in errors["wanted"]


@pytest.mark.parametrize(
    "field,raw",
    [("notify_threshold", "101"), ("notify_threshold", "x"),
     ("digest_interval_hours", "0"), ("digest_interval_hours", "49")],
)
def test_number_fields_have_limits(field, raw):
    _, errors = parse_profile_form(profile_form(**{field: raw}), [])
    assert field in errors


def test_digest_threshold_cannot_be_above_the_urgent_one():
    _, errors = parse_profile_form(profile_form(notify_threshold="30", digest_threshold="50"), [])
    assert "Granica za skupnu obavijest" in errors["digest_threshold"]


def test_quiet_hours_need_both_ends_and_a_real_time():
    assert "quiet_start" in parse_profile_form(profile_form(quiet_start="22:00"), [])[1]
    late = profile_form(quiet_start="kasno", quiet_end="07:00")
    assert "quiet_start" in parse_profile_form(late, [])[1]
    values, errors = parse_profile_form(profile_form(quiet_start="22:00", quiet_end="07:00"), [])
    assert errors == {} and (values.quiet_start, values.quiet_end) == ("22:00", "07:00")


def test_min_pay_must_be_a_number_in_range():
    assert "min_pay" in parse_profile_form(profile_form(min_pay="abc"), [])[1]
    assert "min_pay" in parse_profile_form(profile_form(min_pay="99"), [])[1]
    assert parse_profile_form(profile_form(min_pay=""), [])[0].min_pay is None


def test_save_profile_updates_everything_including_cv_rules(db):
    profile = get_or_create_profile(db)
    hr = Cv(label="HR", language="hr", path="a.pdf", rule={"country": "HR"}, is_default=True)
    en = Cv(label="EN", language="en", path="b.pdf", rule={}, is_default=False)
    profile.cvs.extend([hr, en])
    db.commit()
    form = profile_form(
        wanted=["it"], has_car="on", tone="ti", cv_default=str(en.id),
        **{f"cv_{hr.id}_country": "", f"cv_{hr.id}_keywords": "",
           f"cv_{en.id}_country": "HR", f"cv_{en.id}_keywords": " developer ,software,, "},
    )
    values, errors = parse_profile_form(form, [hr.id, en.id])
    assert errors == {}
    save_profile(db, profile, values)
    db.commit()
    assert (profile.full_name, profile.tone, profile.has_car, profile.min_pay_eur_hour) == (
        "Ivan Primjer", "ti", True, 6.5)
    assert profile.categories_wanted == ["it"]
    assert hr.is_default is False and en.is_default is True
    assert hr.rule == {}
    assert en.rule == {"country": "HR", "keywords_any": ["developer", "software"]}


def test_a_bogus_default_cv_falls_back_to_the_first(db):
    values, _ = parse_profile_form(profile_form(cv_default="999"), [4, 5])
    assert values.default_cv == 4


def test_new_profile_is_formal_vi_by_default(db):
    assert get_or_create_profile(db).tone == "Vi"
    db.commit()
    assert get_or_create_profile(db).tone == "Vi"


# ---------- re-scoring after a change
def add_job(db, *, city, status="new", score=50, alerted=False, ext="1"):
    s = db.query(Source).filter_by(key="r").first() or Source(key="r", name="r")
    lat, lon = lookup_city(city)
    j = Job(source=s, external_id=ext, url="https://x", title="Konobar", employer="Luna",
            city=city, lat=lat, lon=lon, pay_eur_hour=7.5, category="ugostiteljstvo",
            channel="email", status=status, score=score, first_seen_at=NOW - timedelta(minutes=10),
            posted_at=NOW - timedelta(minutes=10), alerted_at=NOW if alerted else None)
    db.add(j)
    db.commit()
    return j


def home(db, minutes=45):
    lat, lon = lookup_city("Osijek")
    p = Place(label="Dom", lat=lat, lon=lon, max_minutes_by_mode={"bus": minutes}, active=True)
    db.add(p)
    db.commit()
    return p


def test_ad_set_aside_as_too_far_comes_back_when_a_new_place_brings_it_in_reach(db):
    profile = get_or_create_profile(db)
    far = add_job(db, city="Đakovo", status="ignored", score=0)
    place = home(db, minutes=45)
    rescore_jobs(db, profile, [place], NOW)
    assert far.status == "ignored" and far.score == 0  # still too far for 45 min by bus
    place.max_minutes_by_mode = {"bus": 150}
    rescore_jobs(db, profile, [place], NOW)
    assert far.status == "new" and far.score > 0 and far.score_reasons


def test_ad_that_becomes_unreachable_is_set_aside(db):
    profile = get_or_create_profile(db)
    job = add_job(db, city="Zagreb", status="new", score=70)
    n = rescore_jobs(db, profile, [home(db)], NOW)
    assert (job.status, job.score) == ("ignored", 0) and n == 1


def test_an_ad_ignored_by_ivan_is_never_revived(db):
    profile = get_or_create_profile(db)
    mine = add_job(db, city="Osijek", status="ignored", score=80)  # score above 0: Ivan's choice
    rescore_jobs(db, profile, [home(db)], NOW)
    assert mine.status == "ignored"


def test_backlog_ads_keep_alerted_at_so_nothing_old_turns_into_an_alert(db):
    profile = get_or_create_profile(db)
    old = add_job(db, city="Osijek", status="ignored", score=0, alerted=True)
    rescore_jobs(db, profile, [home(db)], NOW)
    assert old.status == "new" and old.alerted_at is not None


def test_old_ads_and_duplicates_are_left_alone(db):
    profile = get_or_create_profile(db)
    ancient = add_job(db, city="Osijek", score=1, ext="old")
    ancient.first_seen_at = NOW - timedelta(days=60)
    dup = add_job(db, city="Osijek", score=1, ext="dup")
    dup.duplicate_of = ancient.id
    db.commit()
    assert rescore_jobs(db, profile, [home(db)], NOW) == 0
