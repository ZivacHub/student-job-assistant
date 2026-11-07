"""Light is the default theme; dark only when chosen with the button (it never follows the
computer's own setting, because the computer is set to dark and Ivan prefers white)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.main import create_app

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"


@pytest.fixture
def client(db):
    app = create_app()

    def _session():
        yield db

    app.dependency_overrides[get_session] = _session
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("page", ["/poslovi", "/mjesta"])
def test_every_page_loads_the_theme_script_first_and_has_the_button(client, page):
    html = client.get(page).text
    head = html.split("</head>")[0]
    assert '<script src="/static/theme.js"></script>' in head
    assert head.index("/static/theme.js") < head.index("/static/tokens.css")  # before drawing
    assert '<meta name="color-scheme" content="light">' in head
    assert "data-theme-toggle" in html and "Prebaci na tamnu temu" in html
    assert "Prebaci na svijetlu temu" in html
    assert "<script>" not in html  # the content policy forbids inline scripts


def test_the_button_starts_hidden_so_it_never_does_nothing(client):
    button = client.get("/poslovi").text.split("<button class=\"theme-toggle\"")[1].split(">")[0]
    assert " hidden" in button and 'type="button"' in button


def test_the_script_is_served_and_keeps_the_choice_in_this_browser(client):
    r = client.get("/static/theme.js")
    assert r.status_code == 200
    for part in ('"theme"', "localStorage", "data-theme", "DOMContentLoaded"):
        assert part in r.text
    assert "matchMedia" not in r.text and "prefers-color-scheme" not in r.text


def test_styles_never_follow_the_computers_dark_setting():
    for name in ("tokens.css", "app.css"):
        css = (STATIC / name).read_text(encoding="utf-8")
        assert "prefers-color-scheme" not in css, name
    tokens = (STATIC / "tokens.css").read_text(encoding="utf-8")
    root = tokens.split(":root {", 1)[1].split("\n}", 1)[0]
    assert "color-scheme: light;" in root and "dark" not in root
    assert ':root[data-theme="dark"]' in tokens and "color-scheme: dark;" in tokens


def test_map_pictures_are_darkened_only_in_the_dark_theme():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert css.count(".leaflet-tile-pane") == 1
    assert ':root[data-theme="dark"] .leaflet-tile-pane' in css


def test_theme_script_is_allowed_by_the_content_policy(client):
    r = client.get("/poslovi")
    assert "script-src 'self'" in r.headers["content-security-policy"]
