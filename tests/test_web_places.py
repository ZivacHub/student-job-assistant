"""Step 4.2 (pages): the Mjesta i profil screen and the local-only guard around it."""

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.profile import get_or_create_profile
from app.main import create_app
from app.models import Cv, Place
from app.web.security import CSP, LocalOnlyGuard

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    return TestClient(app, follow_redirects=False)


def place_post(**kw):
    data = {"label": "Dom", "kind": "home", "town": "osijek", "min_bus": "40", "bonus": "0",
            "active": "on"}
    data.update(kw)
    return data


# ---------- reading
def test_page_is_in_croatian_and_has_the_main_parts(client):
    r = client.get("/mjesta")
    assert r.status_code == 200
    for text in ("Mjesta i profil", "Moja mjesta", "Dodaj mjesto", "Vi i posao koji tražite",
                 "Obavijesti", "Životopisi", "Spremi profil", "Još nema nijednog mjesta"):
        assert text in r.text
    assert '<html lang="hr">' in r.text and 'id="map-data"' in r.text


def test_home_redirects_to_the_inbox(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/poslovi"


def test_new_profile_page_preselects_formal_vi(client):
    html = client.get("/mjesta").text
    assert '<option value="Vi" selected>Vi (službeno)</option>' in html


def test_static_files_and_icons_are_served(client):
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/map.js").status_code == 200
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code == 200
    assert client.get("/static/favicon.svg").status_code == 200
    assert client.get("/favicon.ico").status_code == 204


def test_first_place_form_suggests_dom(client):
    assert 'value="Dom"' in client.get("/mjesta").text


# ---------- places
def test_adding_a_place_redirects_and_shows_it(client, db):
    r = client.post("/mjesta/mjesto", data=place_post(), headers=ORIGIN)
    assert r.status_code == 303 and r.headers["location"].startswith("/mjesta?ok=mjesto&n=")
    assert [p.label for p in db.query(Place)] == ["Dom"]
    page = client.get(r.headers["location"]).text
    assert "Mjesto je spremljeno." in page and "Osijek · autobusom 40 min" in page


def test_flash_mentions_how_many_ads_were_rescored(client):
    page = client.get("/mjesta?ok=profil&n=3").text
    assert "Profil je spremljen. Ocjena je ponovno izračunata za 3 oglasa." in page
    assert "za 1 oglas." in client.get("/mjesta?ok=mjesto&n=1").text
    assert "Ocjena je ponovno" not in client.get("/mjesta?ok=mjesto&n=0").text


def test_unknown_flash_codes_show_nothing(client):
    page = client.get("/mjesta?ok=<script>alert(1)</script>&n=5").text
    assert "<script>alert(1)" not in page and 'role="status"' not in page


def test_a_bad_place_comes_back_as_a_normal_page_listing_the_problems(client, db):
    bad = place_post(label="", town="", min_bus="")
    r = client.post("/mjesta/mjesto", data=bad, headers=ORIGIN)
    assert r.status_code == 200
    assert "Mjesto nije spremljeno. Ispravite:" in r.text
    assert "Odaberite grad ili kliknite na kartu" in r.text
    assert 'aria-invalid="true"' in r.text and "data-error-summary" in r.text
    assert 'details class="place place--new" open' in r.text.replace("\n", " ").replace("  ", " ")
    assert db.query(Place).count() == 0


def test_the_typed_values_survive_a_failed_save(client):
    data = place_post(label="Kod djevojke", min_bus="", min_walk="25", town="", lat="45.5",
                      lon="18.7", bonus="99")
    r = client.post("/mjesta/mjesto", data=data, headers=ORIGIN)
    assert 'value="Kod djevojke"' in r.text and 'value="25"' in r.text
    assert "Bonus bodovi moraju biti između 0 i 15" in r.text


def test_editing_a_place_changes_it_and_rescoring_runs(client, db):
    client.post("/mjesta/mjesto", data=place_post(), headers=ORIGIN)
    place = db.query(Place).one()
    data = {**place_post(label="Dom (Osijek)"), "id": str(place.id)}
    r = client.post("/mjesta/mjesto", data=data, headers=ORIGIN)
    assert r.status_code == 303
    db.refresh(place)
    assert place.label == "Dom (Osijek)" and db.query(Place).count() == 1


def test_editing_a_missing_place_says_so_in_plain_words(client):
    r = client.post("/mjesta/mjesto", data={**place_post(), "id": "999"}, headers=ORIGIN)
    assert r.status_code == 200 and "ne postoji" in r.text


def test_deleting_a_place(client, db):
    client.post("/mjesta/mjesto", data=place_post(), headers=ORIGIN)
    place = db.query(Place).one()
    r = client.post(f"/mjesta/mjesto/{place.id}/obrisi", headers=ORIGIN)
    assert r.status_code == 303 and "ok=obrisano" in r.headers["location"]
    assert db.query(Place).count() == 0
    assert client.post("/mjesta/mjesto/999/obrisi", headers=ORIGIN).status_code == 303  # idempotent


def test_text_typed_into_a_place_name_cannot_inject_html(client, db):
    evil = place_post(label="<img src=x onerror=alert(1)>")
    client.post("/mjesta/mjesto", data=evil, headers=ORIGIN)
    page = client.get("/mjesta").text
    assert "<img src=x onerror" not in page and "&lt;img src=x onerror=alert(1)&gt;" in page


def test_map_data_in_the_page_cannot_break_out_of_its_script_tag(client):
    client.post("/mjesta/mjesto", data=place_post(label="</script><script>alert(1)</script>"),
                headers=ORIGIN)
    page = client.get("/mjesta").text
    assert "</script><script>alert(1)" not in page


# ---------- profile
def test_saving_the_profile(client, db):
    r = client.post("/mjesta/profil", data={
        "full_name": "Ivan Primjer", "tone": "Vi", "has_car": "on", "wanted": ["it"],
        "min_pay": "7",
        "notify_threshold": "70", "digest_threshold": "50", "digest_interval_hours": "2",
    }, headers=ORIGIN)
    assert r.status_code == 303 and "ok=profil" in r.headers["location"]
    p = get_or_create_profile(db)
    assert (p.full_name, p.has_car, p.categories_wanted, p.notify_threshold) == (
        "Ivan Primjer", True, ["it"], 70)


def test_profile_errors_list_what_to_fix_and_keep_what_was_typed(client, db):
    r = client.post("/mjesta/profil", data={
        "full_name": "Ivan", "wanted": ["it"], "excluded": ["it"], "notify_threshold": "30",
        "digest_threshold": "50", "digest_interval_hours": "3"}, headers=ORIGIN)
    assert r.status_code == 200
    assert "Profil nije spremljen. Ispravite:" in r.text
    assert "Ista vrsta posla ne može biti i željena i neželjena" in r.text
    assert 'value="Ivan"' in r.text
    assert get_or_create_profile(db).full_name is None


def test_cv_settings_are_shown_and_saved(client, db):
    profile = get_or_create_profile(db)
    profile.cvs.append(Cv(label="Zivotopis_Primjer", language="hr", path="x.pdf", is_default=True))
    db.commit()
    assert "Zivotopis_Primjer" in client.get("/mjesta").text
    cv = profile.cvs[0]
    client.post("/mjesta/profil", data={
        "notify_threshold": "60", "digest_threshold": "40", "digest_interval_hours": "3",
        "cv_default": str(cv.id), f"cv_{cv.id}_country": "HR",
        f"cv_{cv.id}_keywords": "konobar, kafić"}, headers=ORIGIN)
    db.refresh(cv)
    assert cv.rule == {"country": "HR", "keywords_any": ["konobar", "kafić"]}


# ---------- protection from other websites
def test_requests_for_a_foreign_host_name_are_refused(client):
    r = client.get("/mjesta", headers={"host": "evil.example"})
    assert r.status_code == 421


@pytest.mark.parametrize("host", ["localhost", "localhost:8765", "127.0.0.1:8765", "[::1]:8765"])
def test_local_host_names_are_accepted(client, host):
    assert client.get("/mjesta", headers={"host": host}).status_code == 200


def test_a_form_post_without_origin_or_referer_is_refused(client, db):
    r = client.post("/mjesta/mjesto", data=place_post())
    assert r.status_code == 403 and db.query(Place).count() == 0


def test_a_form_post_from_another_site_is_refused(client, db):
    r = client.post("/mjesta/mjesto", data=place_post(), headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and db.query(Place).count() == 0


def test_a_post_from_a_different_port_on_the_same_machine_is_refused(client, db):
    r = client.post("/mjesta/mjesto", data=place_post(), headers={"origin": "http://127.0.0.1:9999"})
    assert r.status_code == 403


def test_referer_is_accepted_when_origin_is_missing(client, db):
    r = client.post("/mjesta/mjesto", data=place_post(),
                    headers={"referer": "http://testserver/mjesta"})
    assert r.status_code == 303


def test_deleting_from_another_site_is_refused(client, db):
    client.post("/mjesta/mjesto", data=place_post(), headers=ORIGIN)
    place = db.query(Place).one()
    r = client.post(f"/mjesta/mjesto/{place.id}/obrisi", headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and db.query(Place).count() == 1


def test_security_headers_are_on_every_response_including_refusals(client):
    responses = (
        client.get("/mjesta"), client.get("/static/app.css"),
        client.post("/mjesta/mjesto", data={}), client.get("/mjesta", headers={"host": "x.y"}),
    )
    for r in responses:
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_csp_allows_only_our_own_scripts_and_openstreetmap_pictures():
    assert "script-src 'self'" in CSP and "unsafe-eval" not in CSP
    assert "img-src 'self' data: https://tile.openstreetmap.org" in CSP
    assert "frame-ancestors 'none'" in CSP and "form-action 'self'" in CSP


def test_the_page_has_no_inline_scripts_that_the_csp_would_block(client):
    page = client.get("/mjesta").text
    scripts = [chunk.split(">", 1)[0] for chunk in page.split("<script")[1:]]
    assert all('src="/static/' in s or 'type="application/json"' in s for s in scripts)


def test_guard_ignores_non_http_traffic():
    calls = []

    async def inner(scope, receive, send):
        calls.append(scope["type"])

    import asyncio

    asyncio.run(LocalOnlyGuard(inner)({"type": "lifespan"}, None, None))
    assert calls == ["lifespan"]
