"""s20 — scripts/console-session-check.sh, the wrapper run by the console job `tiktok-session-check`."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "console-session-check.sh"


def test_script_exists_and_runs_the_module():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert "python -m app.session_check" in text
    assert "/opt/data/console-venvs/tiktok-backoffice" in text
    assert "cd /opt/repos/hermes-social/social/tools/tiktok-backoffice" in text
    assert re.search(r"^exec uv run --frozen --no-dev python -m app\.session_check$", text, re.M)


def test_script_never_prints_the_environment():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^\s*(env|printenv)\b|echo\s+["\']?\$', text, re.M) is None
