"""s23 — scripts/console-tiktok-scan.sh, the wrapper run by the console job `tiktok-scan` (console ADR 054)."""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "console-tiktok-scan.sh"
EXEC = 'exec uv run --frozen --no-dev python -m app.tiktok_scan "$@"'


def test_script_runs_the_module_in_the_console_venv():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert 'export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/tiktok-backoffice}"' in text
    assert re.search(r"^cd /opt/repos/hermes-social/social/tools/tiktok-backoffice$", text, re.M)
    assert re.search(r"^" + re.escape(EXEC) + r"$", text, re.M)


def test_script_no_longer_resolves_a_processor_python():
    # s24 : épingle amendée (processeur retiré du scan, console ADR 055)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "SCAN_PROCESSOR_PYTHON" not in text
    assert "processor" not in text.lower()


def test_script_never_prints_the_environment():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^\s*(env|printenv)\b|echo\s+["\']?\$', text, re.M) is None
    assert "SADCAPTCHA_API_KEY" not in text.split("set -euo pipefail", 1)[1]
