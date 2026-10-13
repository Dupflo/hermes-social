"""TikTok login page: URL, selectors and passive page scripts of the console job `tiktok-login` (s21).

Console ADR 050. This module is the ONLY place that knows TikTok's login DOM, so a selector can be fixed
without touching the login logic (app/tiktok_login.py). None of these selectors could be checked against
the real page (2FA account, production session connected): the first real attempt validates them, and an
unrecognised page ends `login_unknown` with a (masked) screenshot.

Both scripts are passive: no navigation, no click, no value written. MASK_SECRETS_JS only hides
the identifier, password and code fields visually before a screenshot (it never empties them).
"""

from __future__ import annotations

import json

LOGIN_URL = "https://www.tiktok.com/login/phone-or-email/email"

USERNAME_SELECTOR = "input[name=username]"
PASSWORD_SELECTOR = "input[type=password]"
SUBMIT_SELECTOR = "button[data-e2e=login-button]"
CODE_INPUT_SELECTOR = "input[autocomplete=one-time-code]"
CODE_SUBMIT_SELECTOR = "button[type=submit]"

# The three captcha selectors of the story.
CAPTCHA_SELECTORS = (".captcha-verify-container", "#captcha-verify-image", "[data-testid=whirl-inner-img]")

# Error hints (selectors/attributes only, no localised text imposed).
BAD_CREDENTIALS_HINTS = ("[data-e2e=login-error]", "[class*=error-text]", "[role=alert]")

# Identifier fields first, then every way the password field can look (shown as text by the eye icon), then the code.
MASK_SELECTORS = (
    USERNAME_SELECTOR,
    "input[autocomplete=username]",
    "input[type=password]",
    "input[name=password]",
    "input[autocomplete=current-password]",
    CODE_INPUT_SELECTOR,
)


def _list(selectors: tuple[str, ...]) -> str:
    # Selectors carry no double quote: json.dumps keeps them verbatim (tested).
    return json.dumps(list(selectors))


LOGIN_PROBE_JS = (
    "(() => {"
    " const q = (s) => !!document.querySelector(s);"
    " const any = (l) => l.some(q);"
    f" const codeScreen = q({json.dumps(CODE_INPUT_SELECTOR)});"
    f" const hint = any({_list(BAD_CREDENTIALS_HINTS)});"
    " return {"
    f" form: q({json.dumps(USERNAME_SELECTOR)}) && q({json.dumps(PASSWORD_SELECTOR)}),"
    f" captcha: any({_list(CAPTCHA_SELECTORS)}),"
    " code_screen: codeScreen,"
    " bad_credentials: !codeScreen && hint,"
    " code_error: codeScreen && hint"
    " };"
    " })()"
)

MASK_SECRETS_JS = (
    "(() => {"
    " const seen = new Set(); let n = 0;"
    f" for (const s of {_list(MASK_SELECTORS)}) {{"
    " for (const el of document.querySelectorAll(s)) {"
    " if (seen.has(el)) continue; seen.add(el);"
    " el.style.setProperty('-webkit-text-security', 'disc', 'important');"
    " el.style.setProperty('color', 'transparent', 'important');"
    " n += 1;"
    " }"
    " }"
    " return n;"
    " })()"
)
