"""s24 — scripts/console-tiktok-reply.sh, the wrapper run by the console job `tiktok-reply` (console ADR 055)."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "console-tiktok-reply.sh"
EXEC = 'exec uv run --frozen --no-dev python -m app.tiktok_reply "$@"'


def test_script_runs_the_module_in_the_console_venv():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert 'export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/tiktok-backoffice}"' in text
    assert re.search(r"^cd /opt/repos/hermes-social/social/tools/tiktok-backoffice$", text, re.M)
    assert re.search(r"^" + re.escape(EXEC) + r"$", text, re.M)


def test_header_documents_the_contract():
    text = SCRIPT.read_text(encoding="utf-8")
    header = text.split("set -euo pipefail", 1)[0]
    assert "tiktok-browser" in header and "flock" in header
    for code in ("21", "22", "23", "24", "25"):
        assert code in header
    assert "opens no database" in header


def test_script_never_prints_the_environment_nor_runs_a_processor():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^\s*(env|printenv)\b|echo\s+["\']?\$', text, re.M) is None
    body = text.split("set -euo pipefail", 1)[1]
    assert "SADCAPTCHA_API_KEY" not in body
    assert "process_tiktok_review_one" not in text and "python3" not in text
