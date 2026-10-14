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
    assert "# pass 8" in result.stdout
