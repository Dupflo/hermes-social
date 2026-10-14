"""s25 (s24 m2, console ADR 059, P6) — the login gate of app/tiktok_reply_selectors.py.

The JS runs under `node --test tests/js/*.test.mjs` (skipped when node is absent, like test_console_mouse_node_suite);
the source contract it relies on is pinned here.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import tiktok_reply_selectors as sel

ROOT = Path(__file__).resolve().parents[1]


def _definition(name: str) -> str:
    lines = [line for line in sel._HELPERS.splitlines() if line.startswith(f" function {name}(")]
    assert len(lines) == 1, name
    return lines[0]


def test_helpers_used_by_the_node_suite_are_one_line_definitions():
    for name in ("clean", "vis", "loginGate"):
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
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    # s26 : épingle amendée (le glob prend tests/js/reader_guard.test.mjs, 10 cas de plus, console ADR 060)
    # s28 : épingle amendée (le glob prend tests/js/panel_state.test.mjs, 13 cas de plus, console ADR 062)
    assert "# pass 31" in result.stdout


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
