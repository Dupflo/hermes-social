"""s25 (s24 m2, console ADR 059, P6) — the login gate of app/tiktok_reply_selectors.py.

The JS runs under `node --test tests/js/*.test.mjs` (skipped when node is absent, like test_console_mouse_node_suite);
the source contract it relies on is pinned here.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import tiktok_reply_selectors as sel

ROOT = Path(__file__).resolve().parents[1]


def _definition(name: str) -> str:
    lines = [line for line in sel._HELPERS.splitlines() if line.startswith(f" function {name}(")]
    assert len(lines) == 1, name
    return lines[0]


def test_helpers_used_by_the_node_suite_are_one_line_definitions():
    for name in ("clean", "vis", "loginGate", "videoAuthorHandles"):
        line = _definition(name)
        assert line.count("function ") >= 1 and line.rstrip().endswith("}")


def test_login_gate_no_longer_reads_the_page_text():
    gate = _definition("loginGate")
    assert "document.body.innerText" not in gate
    assert "innerText" not in re.sub(r"clean\(el\.innerText\|\|el\.textContent\)", "", gate)
    for container in ('DivCommentItemWrapper', 'DivReplyContainer', 'comment-level', 'chat-item', 'DivChatItemWrapper',
                      'textarea', 'contenteditable', 'textbox'):
        assert container in gate, container


def test_login_gate_node_suite():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", "tests/js/*.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        # s29 (R1): tests/js/video_author.test.mjs exports _COMMENT_EXTRACTION_JS with this interpreter.
        env={**os.environ, "HERMES_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    # s26 : épingle amendée (le glob prend tests/js/reader_guard.test.mjs, 10 cas de plus, console ADR 060)
    # s28 : épingle amendée (le glob prend tests/js/panel_state.test.mjs, 13 cas de plus, console ADR 062)
    # s29 : épingle amendée (le glob prend tests/js/video_author.test.mjs, 8 cas de plus, console ADR 063)
    # s30 : épingle amendée (le glob prend tests/js/dm_path.test.mjs, 26 cas de plus, console ADR 064)
    assert "# pass 65" in result.stdout


def test_reader_guard_is_a_one_line_definition_without_page_text():
    source = (ROOT / "app" / "camofox_reader.py").read_text(encoding="utf-8")
    lines = [line for line in source.splitlines() if line.startswith(" function readerGuard(")]
    assert len(lines) == 1
    line = lines[0]
    assert line.rstrip().endswith("}")
    assert "document.body" not in line and "innerText" not in line
    assert "loginGate()" in line


# --- s28 (console ADR 062, D2): panelState(), the passive snapshot of the right panel ---


def test_panel_state_is_a_one_line_passive_definition():
    line = _definition("panelState")
    assert line.rstrip().endswith("}")
    for forbidden in ("dispatchEvent", ".click(", "scrollIntoView", "location", "innerHTML", "href"):
        assert forbidden not in line, forbidden
    for selector in ('DivCommentListContainer', 'DivCommentContainer', 'DivCommentInputWrapper',
                     'data-e2e="comment-input"', 'DivRelatedTabContainer', 'data-e2e="comment-icon"',
                     'data-e2e="comment-count"', 'tux-tabbar__item-container'):
        assert selector in line, selector


def test_panel_state_js_is_the_helpers_iife():
    assert sel.PANEL_STATE_JS == "(() => {" + sel._HELPERS + " return panelState();})()"


# --- s29 (console ADR 063, D1): videoAuthorHandles(), the handles of the bounded author area of the video ---


def test_video_author_handles_never_queries_every_link_of_the_document():
    line = _definition("videoAuthorHandles")
    assert line.rstrip().endswith("}")
    for forbidden in ("document.querySelectorAll('a[href*=\"/@\"]')", 'document.querySelectorAll("a[href*=\\"/@\\"]")',
                      "document.querySelectorAll('a[href",  'document.querySelectorAll("a[href', "document.links",
                      "dispatchEvent", ".click(", "innerHTML"):
        assert forbidden not in line, forbidden
    for candidate in ('[data-e2e="browse-username"]', '[data-e2e="video-author-uniqueid"]',
                      '[data-e2e="browse-user-avatar"]', '[data-e2e="video-author-avatar"]', '[class*="DivAuthorContainer"]'):
        assert candidate in line, candidate
    for excluded in ("#app-header", "nav", '[data-e2e*="nav"]', '[class*="DivCommentItemWrapper"]',
                     '[class*="DivReplyContainer"]', '[data-e2e^="comment-"]', '[class*="DivRelatedTabContainer"]',
                     '[data-e2e="recommend-list-item-container"]', '[data-e2e="browse-video-desc"]',
                     '[data-e2e="video-desc"]', '[data-e2e="search-common-link"]'):
        assert excluded in line, excluded
    assert "decodeURIComponent" in line and "catch" in line


# --- s30 (console ADR 064, D2-D4, A1, A2, A6, A8): the DM path helpers, in _DM_HELPERS only ---

DM_HELPER_NAMES = ("shapeOf", "messageButton", "dmBlock", "dmEditor", "convColumn", "convText", "sendButton")


def _dm_definition(name: str) -> str:
    lines = [line for line in sel._DM_HELPERS.splitlines() if line.startswith(f" function {name}(")]
    assert len(lines) == 1, name
    return lines[0]


def test_dm_helpers_are_one_line_definitions_outside_the_shared_helpers():
    for name in DM_HELPER_NAMES:
        line = _dm_definition(name)
        assert line.rstrip().endswith("}"), name
        assert f"function {name}(" not in sel._HELPERS, name
    # every line of _DM_HELPERS is a definition (nothing else injected into the two DM scripts)
    assert all(line.startswith(" function ") for line in sel._DM_HELPERS.splitlines() if line.strip())


def test_message_button_excludes_the_navigation_and_never_reads_a_link():
    line = _dm_definition("messageButton")
    assert "#app-header,header,nav,[data-e2e*=\"nav\"]" in line
    assert "href" not in line and "messages\\?" not in line and "messages?" not in line
    assert "'[data-e2e=\"message-button\"],button,[role=\"button\"]'" in line


def test_dm_helpers_only_read_the_page():
    for forbidden in (".click(", "dispatchEvent", "focus(", "scrollIntoView", "innerHTML", "location.href"):
        assert forbidden not in sel._DM_HELPERS, forbidden


def test_dm_helpers_are_injected_into_the_two_dm_scripts_only():
    from app import camofox_reader

    for script in (sel.PROFILE_JS, sel.THREAD_JS):
        assert sel._HELPERS in script and sel._DM_HELPERS in script
    for other in (sel.comments_js("@SENTINEL_OWNER"), sel.PANEL_STATE_JS, sel.REPLY_EDITOR_JS,
                  camofox_reader._COMMENT_EXTRACTION_JS, camofox_reader._READER_GUARD_JS):
        for name in DM_HELPER_NAMES:
            assert f"function {name}(" not in other, name


def test_dm_scripts_only_read_the_page():
    for script in (sel.PROFILE_JS, sel.THREAD_JS):
        for forbidden in (".click(", "dispatchEvent", "focus(", "scrollIntoView", "innerHTML", "location.href"):
            assert forbidden not in script, forbidden
    # the profile script never accepts a link to the inbox: the button comes from messageButton() only
    assert "messages\\?" not in sel.PROFILE_JS and "messages?" not in sel.PROFILE_JS
    assert "message_button:mb.el?handle(mb.el):null" in sel.PROFILE_JS
