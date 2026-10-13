"""s21 — app/tiktok_login_selectors.py: selectors, passive page probe, secret masking, scenario fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import tiktok_login_selectors as sel

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tiktok_login"
EXPECTED_FIXTURES = {
    "already_connected",
    "form_then_connected",
    "form_then_captcha",
    "form_then_bad_credentials",
    "form_then_code_then_connected",
    "code_rejected_then_accepted",
    "code_rejected_three_times",
    "code_timeout",
    "form_missing",
    "captcha_before_form",
}
FIXTURE_KEYS = {"description", "storage_states", "probes", "mask_count", "screenshot_b64", "codes", "expected"}
PROBE_KEYS = {"form", "captcha", "code_screen", "bad_credentials", "code_error"}
EXPECTED_KEYS = {"status", "reason", "attempted", "code_rounds"}
COOKIE_KEYS = {"name", "domain", "value", "expires"}
STORY_CAPTCHA_SELECTORS = (".captcha-verify-container", "#captcha-verify-image", "[data-testid=whirl-inner-img]")


def test_login_url_is_the_email_login_page():
    assert sel.LOGIN_URL == "https://www.tiktok.com/login/phone-or-email/email"


def test_captcha_selectors_are_the_three_of_the_story():
    assert tuple(sel.CAPTCHA_SELECTORS) == STORY_CAPTCHA_SELECTORS
    for selector in STORY_CAPTCHA_SELECTORS:
        assert selector in sel.LOGIN_PROBE_JS


@pytest.mark.parametrize("name", ["LOGIN_PROBE_JS", "MASK_SECRETS_JS"])
def test_scripts_are_passive(name):
    js = getattr(sel, name)
    for banned in ("location", "reload", ".click(", "navigate", "submit("):
        assert banned not in js
    assert re.search(r"\.value\s*=", js) is None


def test_probe_returns_the_five_booleans():
    for key in PROBE_KEYS:
        assert f"{key}:" in sel.LOGIN_PROBE_JS
    for selector in (sel.USERNAME_SELECTOR, sel.PASSWORD_SELECTOR, sel.CODE_INPUT_SELECTOR):
        assert selector in sel.LOGIN_PROBE_JS


def test_mask_covers_username_and_every_password_field():
    js = sel.MASK_SECRETS_JS
    for selector in (
        sel.USERNAME_SELECTOR,
        "input[autocomplete=username]",
        "input[type=password]",
        "input[name=password]",
        "input[autocomplete=current-password]",
        sel.CODE_INPUT_SELECTOR,
    ):
        assert selector in js
    assert "-webkit-text-security" in js and "transparent" in js
    # Review m3: on Firefox (Camoufox) a page-set -webkit-text-fill-color wins over `color`.
    assert "setProperty('-webkit-text-fill-color', 'transparent', 'important')" in js
    assert "return" in js


def test_every_fixture_loads_with_known_keys_only():
    names = {path.stem for path in FIXTURES.glob("*.json")}
    assert names == EXPECTED_FIXTURES
    for path in FIXTURES.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert set(data) <= FIXTURE_KEYS, path.name
        assert {"description", "storage_states", "probes", "expected"} <= set(data), path.name
        assert set(data["expected"]) == EXPECTED_KEYS, path.name
        for probe in data["probes"]:
            assert set(probe) == PROBE_KEYS, path.name
            assert all(isinstance(v, bool) for v in probe.values()), path.name
        for state in data["storage_states"]:
            for cookie in state:
                assert set(cookie) == COOKIE_KEYS, path.name
        for code in data.get("codes", []):
            assert re.fullmatch(r"\d{4,8}", code), path.name
