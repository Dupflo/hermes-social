"""s23 — scripts/console-tiktok-scan.sh, the wrapper run by the console job `tiktok-scan` (console ADR 054)."""

import re
import shutil
import subprocess
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


def test_processor_python_is_resolved_before_uv():
    text = SCRIPT.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    resolve = code.index('if ! SCAN_PROCESSOR_PYTHON="$(command -v python3)"; then')
    export = code.index("export SCAN_PROCESSOR_PYTHON")
    first_uv = next(i for i, line in enumerate(code) if re.search(r"\buv\b", line))
    assert resolve < export < first_uv == code.index(EXEC)


def test_missing_python3_fails_the_run_with_exit_19(tmp_path):
    # review n°1 m3: a container without python3 must not give `ok` runs whose items all end `processor_missing`.
    # Empty PATH: `command -v python3` fails; uv is never reached (exit 127 if it were). The processor never runs.
    bash = shutil.which("bash")
    proc = subprocess.run([bash, str(SCRIPT)], env={"PATH": str(tmp_path)}, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 19
    assert proc.stdout.strip().splitlines() == ["tiktok-scan: error=internal processor_python=missing"]
    assert proc.stderr == ""


def test_script_never_prints_the_environment():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^\s*(env|printenv)\b|echo\s+["\']?\$', text, re.M) is None
    assert "SADCAPTCHA_API_KEY" not in text.split("set -euo pipefail", 1)[1]
