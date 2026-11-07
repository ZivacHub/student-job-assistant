"""Phase 6: tests for the places the coverage report showed no test reaching, chosen by risk:
the robots.txt rules (we must never fetch what a site forbids), the wording of the log lines the
apply engine writes, the "not found" answers of the apply and job routes, and the English draft."""

from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.apply.texts import DraftFacts, build_draft_text
from app.core.db import get_session
from app.main import create_app
from app.sources.http import PoliteClient, RobotsDisallowed, SourceError, _retry_after
from tests.test_event_log import log_event, make_job, make_source, texts

ORIGIN = {"origin": "http://testserver"}
SITE = "https://example.test"


def polite() -> PoliteClient:
    return PoliteClient(min_interval_s=1.0, user_agent="TestBot/1.0", http=httpx.Client(),
                        sleep=lambda s: None, clock=lambda: 1000.0)


# ---------------------------------------------------------------- robots.txt
@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_a_site_that_refuses_robots_txt_is_treated_as_closed(status):
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(status))
    page = respx.get(f"{SITE}/poslovi").mock(return_value=httpx.Response(200, text="x"))
    with pytest.raises(RobotsDisallowed):
        polite().get(f"{SITE}/poslovi")
    assert page.call_count == 0


@respx.mock
def test_a_site_with_no_robots_txt_has_no_restrictions():
    respx.get(f"{SITE}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{SITE}/poslovi").mock(return_value=httpx.Response(200, text="ok"))
    assert polite().get(f"{SITE}/poslovi").text == "ok"


@respx.mock
def test_if_robots_txt_cannot_be_read_nothing_is_fetched():
    respx.get(f"{SITE}/robots.txt").mock(side_effect=httpx.ConnectError("down"))
    page = respx.get(f"{SITE}/poslovi").mock(return_value=httpx.Response(200, text="x"))
    with pytest.raises(SourceError, match="robots.txt"):
        polite().get(f"{SITE}/poslovi")
    assert page.call_count == 0


def test_a_retry_after_that_is_not_a_number_falls_back_to_30_seconds():
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "soon"})) == 30.0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "7"})) == 7.0


# ---------------------------------------------------------------- the log wording
def test_the_apply_engine_writes_plain_croatian_log_lines(db):
    src = make_source(db)
    job = make_job(db, src, title="Konobar vikend")
    kinds = ["job_called", "draft_created", "draft_edited", "application_approved",
             "application_undone", "application_skipped", "application_reopened",
             "application_released", "application_confirmed", "application_dry_run",
             "application_sent", "application_blocked", "application_failed"]
    for minute, kind in enumerate(kinds, start=1):
        log_event(db, kind, job, minute)
    log_event(db, "apply_rules_changed", None, 20, daily_cap=10, cooldown_days=14,
              undo_minutes=2)
    log_event(db, "sending_paused", None, 21)
    log_event(db, "sending_resumed", None, 22)
    lines = texts(db)
    assert len(lines) == len(kinds) + 3
    assert not any(line.startswith("Događaj:") for line in lines)  # none falls back to the raw name
    joined = "\n".join(lines)
    assert "najviše 10 na dan, razmak 14 dana, poništavanje 2 min" in joined
    assert "Vi ste zaustavili slanje." in lines and "Vi ste ponovno dopustili slanje." in lines
    assert "Automatski je pripremljen nacrt poruke" in joined
    assert "nije poslana jer pravila slanja to ne dopuštaju" in joined
    assert "Online ugovor radite sami" in joined  # the reminder after the employer says yes


def test_a_draft_created_on_request_says_so(db):
    src = make_source(db)
    job = make_job(db, src, title="Konobar vikend")
    log_event(db, "draft_created", job, 1, manual=True)
    assert texts(db) == ["Na Vaš zahtjev je pripremljen nacrt poruke za oglas „Konobar vikend“."]


# ---------------------------------------------------------------- the English draft
def test_the_english_draft_mentions_a_car_only_for_a_job_where_it_matters():
    base = dict(title="Courier", employer="Fast d.o.o.", availability="weekends",
                signature="Ivan Primjer", cv_language="en", has_car=True)
    courier = build_draft_text(DraftFacts(category="dostava", **base))
    waiter = build_draft_text(DraftFacts(category="ugostiteljstvo", **base))
    assert courier.language == "en" and "I have my own car." in courier.body
    assert "car" not in waiter.body


# ---------------------------------------------------------------- not found, not a crash
@pytest.fixture
def web(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    app.state.scheduler = None
    return SimpleNamespace(client=TestClient(app, follow_redirects=False))


@pytest.mark.parametrize("path", ["spremi", "odobri", "ponisti", "preskoci", "vrati",
                                  "probno-vrati", "potvrdeno"])
def test_acting_on_a_draft_that_does_not_exist_gives_not_found(web, path):
    r = web.client.post(f"/prijave/999999/{path}", headers=ORIGIN, data={})
    assert r.status_code == 404


@pytest.mark.parametrize("path", ["/prijave/abc", "/prijave/99999999999999", "/poslovi/abc",
                                  "/poslovi/999999"])
def test_a_page_for_something_that_does_not_exist_gives_not_found(web, path):
    assert web.client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["nacrt", "poslodavac"])
def test_acting_on_a_job_that_does_not_exist_gives_not_found(web, path):
    r = web.client.post(f"/poslovi/999999/{path}", headers=ORIGIN, data={})
    assert r.status_code == 404


def test_a_post_without_the_right_origin_is_refused(web):
    r = web.client.post("/prijave/999999/odobri", data={})
    assert r.status_code in (400, 403)
