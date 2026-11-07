"""Step 4.9 (G5): scoring and the Inbox use street times, and fall back to the rough estimate."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.scoring.rescore as rescore_module
from app.core.db import get_session
from app.core.inbox import _travel
from app.core.profile import get_or_create_profile
from app.geo.streets import INF, StreetMap
from app.geo.travel import accesses
from app.main import create_app
from app.models import Job, JobStatus, Place, Source
from app.models.base import utcnow
from app.notify.alerts import reasons_hr
from app.scoring.engine import score_job
from app.scoring.rescore import rescore_jobs
from tests.street_fixtures import doc, line, pt, town, way
from tests.test_tram import grid_streets, tram_lines

HOME = pt(2, 3)  # west bank
ACROSS = pt(8, 3)  # east bank: 600 m away in a straight line, 1200 m on foot over the bridge


def street_map() -> StreetMap:
    return StreetMap.from_osm([town()])


def place(minutes: dict, **kw):
    return SimpleNamespace(label="Dom", lat=HOME[0], lon=HOME[1], max_minutes_by_mode=minutes,
                           bonus_points=0, active=True, **kw)


def profile(**kw):
    values = dict(weights=None, categories_wanted=[], categories_excluded=[], min_pay_eur_hour=None,
                  has_car=True)
    values.update(kw)
    return SimpleNamespace(**values)


def job(at=ACROSS, **kw):
    now = utcnow()
    values = dict(category=None, pay_eur_hour=None, lat=at[0], lon=at[1], posted_at=now,
                  first_seen_at=now, channel="email")
    values.update(kw)
    return SimpleNamespace(**values)


def travel_reason(result):
    return next(r for r in result.reasons if r["k"] == "travel")


# ---------------------------------------------------------------- the river changes the answer
def test_a_job_across_the_river_was_in_reach_by_the_old_guess_and_is_not_by_the_streets():
    places = [place({"walk": 12})]
    old = score_job(job(), profile(), places)
    real = score_job(job(), profile(), places, streets=street_map())
    assert not old.excluded  # 600 m x 1.3 at 5 km/h = 9.4 minutes
    assert real.excluded and "Too far: 15 min by walk" in real.reasons[0]["text"]


def test_with_enough_minutes_the_street_time_is_used_and_marked_as_real():
    result = score_job(job(), profile(), [place({"walk": 20})], streets=street_map())
    reason = travel_reason(result)
    assert reason["data"]["minutes"] == 15 and reason["data"]["mode"] == "walk"
    assert reason["data"]["approx"] is False
    assert "rough" not in reason["text"]


def test_without_a_map_the_estimate_is_used_and_marked_as_rough():
    result = score_job(job(), profile(), [place({"walk": 20})])
    reason = travel_reason(result)
    assert reason["data"]["approx"] is True and "(rough estimate)" in reason["text"]


def test_outside_the_downloaded_map_the_rough_estimate_is_used():
    near_but_off_the_map = pt(2, 3 + 12)  # 1.2 km north of the last street: no street within reach
    result = score_job(job(at=near_but_off_the_map), profile(), [place({"walk": 30})],
                       streets=street_map())
    reason = travel_reason(result)
    assert reason["data"]["approx"] is True and not result.excluded


def test_a_way_the_map_does_not_model_is_estimated():
    [access] = accesses(*ACROSS, [place({"bus": 60})], True, street_map())
    assert access.mode == "bus" and access.approx is True


def test_street_and_rough_times_can_be_mixed_per_way_of_travelling():
    accs = accesses(*ACROSS, [place({"walk": 60, "bus": 60})], True, street_map())
    assert {(a.mode, a.approx) for a in accs} == {("walk", False), ("bus", True)}


# ---------------------------------------------------------------- no way at all
def test_a_way_with_no_route_never_counts_as_reachable():
    # the way down from the west street is one-way northbound and the other way down is gone:
    # a car standing at home cannot get anywhere near the east bank
    elements = [e for e in town()["elements"] if e["id"] not in (6, 8)]
    trapped = StreetMap.from_osm([doc(*elements, way(6, line(2, 0, 2, 3), oneway="yes"))])
    [access] = accesses(*ACROSS, [place({"car": 60})], True, trapped)
    assert access.minutes == INF and not access.reachable
    result = score_job(job(), profile(), [place({"car": 60})], streets=trapped)
    assert result.excluded and "No way to get there" in result.reasons[0]["text"]


def test_the_car_is_ignored_without_a_car_also_on_the_street_map():
    assert accesses(*ACROSS, [place({"car": 60})], False, street_map()) == []


# ---------------------------------------------------------------- the tram
def test_the_tram_counts_with_its_own_label_and_is_not_marked_rough():
    sm = StreetMap.from_osm([doc(*grid_streets(), *tram_lines())])
    home = SimpleNamespace(label="Dom", lat=pt(0, 6)[0], lon=pt(0, 6)[1],
                           max_minutes_by_mode={"tram": 15}, bonus_points=0, active=True)
    result = score_job(job(at=pt(12, 6)), profile(), [home], streets=sm)
    reason = travel_reason(result)
    assert reason["data"]["mode"] == "tram" and reason["data"]["approx"] is False
    assert reason["data"]["minutes"] == 11  # 6 minutes of waiting + the ride


def test_without_a_map_the_tram_is_estimated_from_the_straight_line():
    home = place({"tram": 30})
    [access] = accesses(*ACROSS, [home], True, None)
    assert access.mode == "tram" and access.approx is True
    assert access.minutes == pytest.approx(600 * 1.3 / 1000 / 18 * 60 + 12, abs=0.1)


# ---------------------------------------------------------------- rescoring and the screens
def make_rows(db, minutes):
    src = Source(key="t", name="studentski-poslovi.hr")
    db.add(src)
    prof = get_or_create_profile(db)
    prof.has_car = False
    home = Place(label="Dom", kind="home", lat=HOME[0], lon=HOME[1], max_minutes_by_mode=minutes,
                 active=True)
    db.add(home)
    row = Job(source=src, external_id="e1", url="https://example.hr/1", title="Konobar",
              status=JobStatus.NEW.value, channel="email", lat=ACROSS[0], lon=ACROSS[1],
              first_seen_at=utcnow() - timedelta(minutes=5))
    db.add(row)
    db.flush()
    return prof, home, row


def test_rescoring_with_a_street_map_sets_a_job_across_the_river_aside(db):
    prof, home, row = make_rows(db, {"walk": 12})
    assert rescore_jobs(db, prof, [home], streets=None) == 1  # the rough estimate: in reach
    assert row.status == JobStatus.NEW.value and row.score > 0
    rescore_jobs(db, prof, [home], streets=street_map())
    assert row.status == JobStatus.IGNORED.value and row.score == 0


def test_rescoring_uses_the_map_downloaded_to_this_pc_unless_told_otherwise(db, monkeypatch):
    prof, home, row = make_rows(db, {"walk": 12})
    sm = street_map()
    monkeypatch.setattr(rescore_module, "get_street_map", lambda: sm)
    rescore_jobs(db, prof, [home])
    assert row.status == JobStatus.IGNORED.value


def travel_data(minutes, mode="walk", **extra):
    return [{"k": "travel", "data": {"minutes": minutes, "mode": mode, "place": "Dom", **extra}}]


def test_the_inbox_knows_when_a_time_is_only_a_rough_estimate():
    rough = travel_data(9, approx=True)
    real = travel_data(15, approx=False)
    old = travel_data(9)  # stored before the street map existed
    assert _travel(rough) == (9, "pješice od: Dom", False, True)
    assert _travel(real) == (15, "pješice od: Dom", False, False)
    assert _travel(old)[3] is False
    assert _travel([{"k": "travel", "data": {"unknown": True}}]) == (None, None, True, False)


def test_the_alert_lines_say_oko_for_a_rough_time():
    rough = travel_data(9, approx=True)
    tram = travel_data(11, "tram")
    assert reasons_hr(rough) == ["oko 9 min pješice od: Dom"]
    assert reasons_hr(tram) == ["11 min tramvajem od: Dom"]


def test_the_inbox_page_shows_oko_only_for_a_rough_time(db):
    prof, home, row = make_rows(db, {"walk": 30})
    rescore_jobs(db, prof, [home], streets=None)
    app = create_app()
    app.dependency_overrides[get_session] = lambda: (yield db)
    client = TestClient(app, follow_redirects=False)
    rough = client.get("/poslovi").text
    assert "<strong>oko 9 min</strong> pješice od: Dom" in rough
    rescore_jobs(db, prof, [home], streets=street_map())
    real = client.get("/poslovi").text
    assert "<strong>15 min</strong> pješice od: Dom" in real
    assert "oko 15" not in real
