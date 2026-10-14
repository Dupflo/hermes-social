"""s22 — app/captcha_selectors.py: the only module that knows the captcha DOM; passive page scripts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import captcha_geometry as geo
from app import captcha_selectors as sel

APP = Path(__file__).resolve().parents[1] / "app"
BANNED = ("location", "reload", ".click(", "dispatchEvent", "MouseEvent", "navigate")


def _scripts() -> dict[str, str]:
    scripts = {"PRESENT_JS": sel.PRESENT_JS, "FACTS_JS": sel.FACTS_JS, "REFRESH_JS": sel.REFRESH_JS}
    for variant in geo.VARIANTS:
        scripts[f"geometry_js({variant})"] = sel.geometry_js(variant)
        for selector in sel.image_selectors(variant):
            scripts[f"image_js({selector})"] = sel.image_js(selector)
    return scripts


@pytest.mark.parametrize("name", sorted(_scripts()))
def test_scripts_are_passive(name):
    js = _scripts()[name]
    for banned in BANNED:
        assert banned not in js, banned
    assert re.search(r"\.value\s*=", js) is None


def test_present_script_looks_for_both_wrappers():
    assert ".captcha-disable-scroll" in sel.PRESENT_JS
    assert ".captcha-verify-container" in sel.PRESENT_JS


def test_facts_script_carries_every_identifier():
    for selector in sel.IDENTIFIERS.values():
        assert json.dumps(selector) in sel.FACTS_JS
    for key in ("present", "puzzle_v1", "puzzle_v2", "rotate_v1", "rotate_v2", "submit_v1", "submit_v2",
                "img_src_kind", "challenge"):
        assert key in sel.FACTS_JS
    assert '"/3d"' in sel.FACTS_JS and '"/icon"' in sel.FACTS_JS
    assert "200" in sel.FACTS_JS  # challenge truncated


def test_identifiers_match_the_research_table():
    assert sel.IDENTIFIERS == {
        "puzzle_v1": ".captcha-disable-scroll img.captcha_verify_img_slide",
        "puzzle_v2": ".captcha-verify-container #captcha-verify-image",
        "rotate_v1": ".captcha-disable-scroll [data-testid=whirl-inner-img]",
        "rotate_v2": ".captcha-verify-container > div > div > div > img.cap-absolute",
        "submit_v1": ".captcha-disable-scroll .verify-captcha-submit-button",
        "submit_v2": ".captcha-verify-container .cap-relative button.cap-w-full",
    }


def test_every_variant_has_its_elements():
    for variant in geo.VARIANTS:
        kind, _ = geo.variant_parts(variant)
        elements = sel.ELEMENTS[variant]
        if kind == "puzzle":
            assert {"puzzle", "piece", "handle"} <= set(elements)
        elif kind == "rotate":
            assert {"outer", "inner", "bar", "handle"} <= set(elements)
        else:
            assert {"image", "submit"} <= set(elements)
        if kind == "icon":
            assert "challenge" in elements
    assert "watch" in sel.ELEMENTS["puzzle_v2"]
    assert sel.REFRESH_BUTTON == "#captcha_refresh_button"


def test_image_js_escapes_the_selector_with_json_dumps():
    tricky = 'img[alt="a\\"b"]'
    js = sel.image_js(tricky)
    assert json.dumps(tricky) in js
    assert "async" in js and "FileReader" in js and "fetch(" in js
    assert str(sel.IMAGE_MAX_BYTES) in js
    assert sel.IMAGE_MAX_BYTES == 2 * 1024 * 1024


def test_geometry_js_reads_translate_x_for_puzzle_v2_only():
    assert "translateX" in sel.geometry_js("puzzle_v2")
    assert "translate_x" in sel.geometry_js("puzzle_v2")
    assert "translateX" not in sel.geometry_js("rotate_v1")
    with pytest.raises(KeyError):
        sel.geometry_js("nope")


@pytest.mark.parametrize("module", ["captcha_selectors.py", "captcha_geometry.py"])
def test_no_sadcaptcha_url_in_dom_modules(module):
    source = (APP / module).read_text(encoding="utf-8").lower()
    assert "sadcaptcha.com" not in source and "licensekey" not in source


@pytest.mark.parametrize("module", ["captcha_selectors.py", "captcha_geometry.py"])
def test_dom_modules_have_no_forbidden_call(module):
    # Plan s22 § Run interdicts: never a navigation, a reload, a tab deletion, a key press or a print.
    source = (APP / module).read_text(encoding="utf-8")
    for banned in ("navigate(", "location.", "reload", '"/refresh"', "delete_tab", "press(", "print("):
        assert banned not in source, banned
    # s25 : épingle amendée (rafraîchissement relu, console ADR 059): REFRESH_JS only reads a rectangle
    assert "refresh" not in source.replace("#captcha_refresh_button", "").replace("REFRESH_JS", "")


# s27 (console ADR 061): geometry_js of every variant but rotate_v2, frozen at e47d7ae (before the held probe).
GEOMETRY_JS_E47D7AE = {
    'puzzle_v1': (
        '(() => { const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibili'
        "ty !== 'hidden'; const all = (s) => Array.from(document.querySelectorAll(s)); const first = (s) => a"
        'll(s).find(vis) || null; const any = (s) => all(s).some(vis); const rect = (s) => { const el = first'
        '(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.w'
        'idth, height: r.height }; }; const out = { rects: { puzzle: rect("#captcha-verify-image"), handle: r'
        'ect(".secsdk-captcha-drag-icon"), } }; return out; })()'
    ),
    'puzzle_v2': (
        '(() => { const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibili'
        "ty !== 'hidden'; const all = (s) => Array.from(document.querySelectorAll(s)); const first = (s) => a"
        'll(s).find(vis) || null; const any = (s) => all(s).some(vis); const rect = (s) => { const el = first'
        '(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.w'
        'idth, height: r.height }; }; const out = { rects: { puzzle: rect("#captcha-verify-image"), handle: r'
        'ect(".secsdk-captcha-drag-icon"), } }; const w = first(".captcha-verify-container div[draggable=true'
        ']:has(img[draggable=false])") || document.querySelector(".captcha-verify-container div[draggable=tru'
        'e]:has(img[draggable=false])"); const m = w ? /translateX\\((-?\\d+(?:\\.\\d+)?)/.exec(String(w.getAttri'
        "bute('style') || '')) : null; out.translate_x = m ? Math.trunc(Number(m[1])) : null; return out; })("
        ')'
    ),
    'rotate_v1': (
        '(() => { const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibili'
        "ty !== 'hidden'; const all = (s) => Array.from(document.querySelectorAll(s)); const first = (s) => a"
        'll(s).find(vis) || null; const any = (s) => all(s).some(vis); const rect = (s) => { const el = first'
        '(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.w'
        'idth, height: r.height }; }; const out = { rects: { bar: rect(".captcha_verify_slide--slidebar"), ha'
        'ndle: rect(".secsdk-captcha-drag-icon"), } }; return out; })()'
    ),
    'shapes_v1': (
        '(() => { const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibili'
        "ty !== 'hidden'; const all = (s) => Array.from(document.querySelectorAll(s)); const first = (s) => a"
        'll(s).find(vis) || null; const any = (s) => all(s).some(vis); const rect = (s) => { const el = first'
        '(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.w'
        'idth, height: r.height }; }; const out = { rects: { image: rect("#captcha-verify-image"), } }; retur'
        'n out; })()'
    ),
    'shapes_v2': (
        '(() => { const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibili'
        "ty !== 'hidden'; const all = (s) => Array.from(document.querySelectorAll(s)); const first = (s) => a"
        'll(s).find(vis) || null; const any = (s) => all(s).some(vis); const rect = (s) => { const el = first'
        '(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.w'
        'idth, height: r.height }; }; const out = { rects: { image: rect(".captcha-verify-container div.cap-r'
        'elative img"), } }; return out; })()'
    ),
    'icon_v1': (
        '(() => { const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibili'
        "ty !== 'hidden'; const all = (s) => Array.from(document.querySelectorAll(s)); const first = (s) => a"
        'll(s).find(vis) || null; const any = (s) => all(s).some(vis); const rect = (s) => { const el = first'
        '(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.w'
        'idth, height: r.height }; }; const out = { rects: { image: rect("#captcha-verify-image"), } }; retur'
        'n out; })()'
    ),
    'icon_v2': (
        '(() => { const vis = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibili'
        "ty !== 'hidden'; const all = (s) => Array.from(document.querySelectorAll(s)); const first = (s) => a"
        'll(s).find(vis) || null; const any = (s) => all(s).some(vis); const rect = (s) => { const el = first'
        '(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.w'
        'idth, height: r.height }; }; const out = { rects: { image: rect(".captcha-verify-container div.cap-r'
        'elative img"), } }; return out; })()'
    ),
}

PROBE_KEYS = ("track", "inner", "outer", "in_nat", "out_nat", "rot_in", "rot_out", "rsrc_in", "rsrc_out", "pair")


def test_geometry_js_of_the_other_variants_is_unchanged():
    # s27 (console ADR 061): the held probe is reserved to rotate v2; every other script is the e47d7ae one.
    assert set(GEOMETRY_JS_E47D7AE) == set(geo.VARIANTS) - {"rotate_v2"}
    for variant, script in GEOMETRY_JS_E47D7AE.items():
        assert sel.geometry_js(variant) == script, variant


def test_rotate_v2_geometry_carries_the_probe():
    js = sel.geometry_js("rotate_v2")
    assert "probe" in js
    for key in PROBE_KEYS:
        assert f" {key}:" in js, key
    for needle in ("parentElement", "naturalWidth", "getComputedStyle", "atan2", "rotate\\(", "matrix"):  # rotate( in a regex
        assert needle in js, needle
    for value in ("'inline'", "'matrix'", "'parent'", "'none'", "'same'", "'ok'"):
        assert value in js, value
    # rects() still only needs bar and handle
    assert " bar: rect(" in js and " handle: rect(" in js
    assert sel.rect_names("rotate_v2") == ("bar", "handle")


@pytest.mark.parametrize("variant", sorted(set(geo.VARIANTS) - {"rotate_v2"}))
def test_the_probe_is_rotate_v2_only(variant):
    js = sel.geometry_js(variant)
    assert "probe" not in js
    for key in ("rot_in", "rot_out", "rsrc_in", "rsrc_out", "in_nat", "out_nat", "pair", "track"):
        assert key not in js, key


def test_rotate_v2_probe_returns_numbers_and_codes_only():
    js = sel.geometry_js("rotate_v2")
    assert "translateX" not in js and "translate_x" not in js and "refresh" not in js
    # the image sources are only compared, never returned
    assert re.search(r"[{,]\s*(src|currentSrc)\s*[:,}]", js) is None
    assert "currentSrc" in js
