"""Step 4.5: the motion and phone-feel rules, written as tests so they cannot quietly break.

They are the animation review's "never ship" list and the mobile-native baseline, checked on the
style sheets and the page shell: nothing here needs a browser.
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"
CSS = (STATIC / "app.css").read_text(encoding="utf-8") + (STATIC / "tokens.css").read_text(
    encoding="utf-8")
MAX_UI_MS = 300
ALLOWED_LONG = {"pulse"}  # the only effect longer than 300 ms: a looping "still working" dot


def blocks(css: str, header: str) -> list[str]:
    """Bodies of every `@media ...{ ... }` block whose header starts with `header`."""
    found, pos = [], 0
    while (start := css.find(header, pos)) != -1:
        open_at = css.index("{", start)
        depth, i = 1, open_at + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        found.append(css[open_at + 1:i - 1])
        pos = i
    return found


def without(css: str, blocks_to_drop: list[str]) -> str:
    for body in blocks_to_drop:
        css = css.replace(body, "")
    return css


def test_nothing_transitions_all_or_scales_from_zero_or_eases_in():
    assert not re.search(r"transition:\s*all\b", CSS)
    assert "transition-property: all" not in CSS
    assert not re.search(r"scale\(\s*0\s*\)", CSS)
    assert not re.search(r"\bease-in\b(?!-)", CSS)  # ease-in-out is fine, ease-in is not


def test_the_strong_ease_out_is_defined_once_and_used_instead_of_the_built_in_one():
    assert CSS.count("--ease-out: cubic-bezier(0.23, 1, 0.32, 1);") == 1
    stripped = CSS.replace("var(--ease-out)", "").replace("--ease-out:", "")
    for use in re.findall(r"(?:transition|animation)[^;{}]*;", stripped):
        assert not re.search(r"\bease-out\b", use), use


def test_only_properties_that_do_not_move_layout_are_animated():
    for use in re.findall(r"transition:([^;}]*)[;}]", CSS):
        for prop in re.findall(r"(?:^|,)\s*([a-z-]+)\s", use.replace("!important", "")):
            assert prop in {"none", "background-color", "color", "border-color", "transform",
                            "opacity", "fill", "stroke"}, (prop, use)


def test_every_duration_is_short_except_the_working_dot():
    for use in re.findall(r"(?:transition|animation):([^;}]*)[;}]", CSS):
        names = set(re.findall(r"\b(?:pulse|message-in|fade-in)\b", use))
        for number, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s)\b", use):
            ms = float(number) * (1000 if unit == "s" else 1)
            if ms > MAX_UI_MS:
                assert names & ALLOWED_LONG, f"too long: {use}"
    assert re.search(r"animation:\s*pulse 1\.4s linear infinite", CSS)  # constant motion = linear


def test_every_hover_rule_is_only_for_pointers_that_can_hover():
    hover_blocks = blocks(CSS, "@media (hover: hover) and (pointer: fine)")
    outside = without(CSS, hover_blocks)
    assert ":hover" not in outside
    assert len(hover_blocks) >= 9


def test_reduced_motion_keeps_fades_and_drops_movement():
    [reduced] = blocks(CSS, "@media (prefers-reduced-motion: reduce)")
    assert "fade-in" in reduced and "transform: none" in reduced
    assert "animation: none" in reduced and "transition: none" in reduced
    assert "message-in" not in reduced  # the message that moves is replaced, not kept


def test_press_feedback_is_instant_and_small():
    assert ".btn:active:not([disabled]) { transform: scale(0.97); }" in CSS
    assert "transform 100ms var(--ease-out)" in CSS


def test_phone_feel_baseline():
    assert "-webkit-tap-highlight-color: transparent" in CSS
    assert "touch-action: manipulation" in CSS
    assert "user-select: none" in CSS and "-webkit-touch-callout: none" in CSS
    assert "@media (pointer: coarse) { input, textarea, select { font-size: 16px; } }" in CSS
    body_rule = re.search(r"(?m)^body \{[^}]*\}", CSS).group(0)
    assert "user-select" not in body_rule  # text content must stay selectable
    assert "env(safe-area-inset-top" in CSS and "env(safe-area-inset-left" in CSS
    assert "env(safe-area-inset-right" in CSS and "env(safe-area-inset-bottom" in CSS
    assert "100vh" not in CSS  # an app shell would use dvh; nothing here needs the tall unit


def test_the_page_shell_does_not_block_zooming_and_sets_the_phone_bar_colour():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    viewport = re.search(r'<meta name="viewport" content="([^"]*)"', base).group(1)
    assert "viewport-fit=cover" in viewport and "width=device-width" in viewport
    for bad in ("user-scalable", "maximum-scale", "minimum-scale"):
        assert bad not in viewport
    assert '<meta name="theme-color" content="#ffffff">' in base
    assert base.index('name="theme-color"') < base.index("/static/theme.js")  # the script edits it


def test_the_theme_script_changes_the_bar_colour_and_fades_the_colours():
    js = (STATIC / "theme.js").read_text(encoding="utf-8")
    assert 'meta[name="theme-color"]' in js and "#171b23" in js and "#ffffff" in js
    assert "theme-changing" in js and "260" in js
