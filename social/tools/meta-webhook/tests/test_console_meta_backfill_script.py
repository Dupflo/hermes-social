"""s10-meta-backfill-job — wrapper run by the hermes-console worker (job `meta-backfill`, ADR 011/022).

The wrapper must run the repo's `app.meta_cron_verify` with absolute --env-file/--db (never the decoys of
the project dir), refuse to start while the Hermes cron copy is running (exit 5), and leave the module's
output as the last line.
"""

import os
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "console-meta-backfill.sh"
RESULT_LINE = 'CONSOLE_RESULT {"status":"ok","counters":{"examined":3}}'
HERMES_CRON_PATTERN = "/opt/data/scripts/meta_cron_verify.py"

FAKE_UV = """#!/usr/bin/env bash
echo "$PWD|$*" >> "$FAKE_UV_LOG"
case "$*" in
  *" -c "*) echo "console: module=$PWD/app/meta_cron_verify.py" ;;
  *" -m app.meta_cron_verify"*)
    echo "console: db=x env_file=y"
    echo 'report {}'
    echo 'counters examined=3'
    if [ "${FAKE_UV_EXIT:-0}" != "0" ]; then
      exit "$FAKE_UV_EXIT"
    fi
    echo '""" + RESULT_LINE + """' ;;
esac
"""

FAKE_PGREP = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_PGREP_LOG"
if [ "${FAKE_PGREP_FOUND:-0}" = "1" ]; then
  echo 4242
  exit 0
fi
exit 1
"""


def _run(tmp_path: Path, *, exit_code: int = 0, hermes_running: bool = False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("uv", FAKE_UV), ("pgrep", FAKE_PGREP)):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    uv_log = tmp_path / "uv.log"
    pgrep_log = tmp_path / "pgrep.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT": str(project),
        "META_ENV_FILE": "/abs/meta/.env",
        "META_DB": "/abs/meta/data/processed_comments.sqlite3",
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        "FAKE_UV_LOG": str(uv_log),
        "FAKE_UV_EXIT": str(exit_code),
        "FAKE_PGREP_LOG": str(pgrep_log),
        "FAKE_PGREP_FOUND": "1" if hermes_running else "0",
    }
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    calls = uv_log.read_text(encoding="utf-8").splitlines() if uv_log.exists() else []
    pgrep_calls = pgrep_log.read_text(encoding="utf-8").splitlines() if pgrep_log.exists() else []
    return proc, calls, pgrep_calls, project


def test_runs_the_module_with_absolute_paths_from_the_project_and_ends_with_its_output(tmp_path):
    proc, calls, pgrep_calls, project = _run(tmp_path)

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 0, proc.stderr
    assert lines[-1] == RESULT_LINE
    module_idx = next(i for i, line in enumerate(lines) if line.startswith("console: module="))
    assert module_idx < lines.index("console: db=x env_file=y")
    assert len(calls) == 2
    assert all(call.split("|", 1)[0] == str(project) for call in calls)
    args = calls[1].split("|", 1)[1].split()
    env_file = args[args.index("--env-file") + 1]
    db = args[args.index("--db") + 1]
    assert env_file == "/abs/meta/.env" and Path(env_file).is_absolute()
    assert db == "/abs/meta/data/processed_comments.sqlite3" and Path(db).is_absolute()
    assert "--frozen" in args and "--no-dev" in args
    assert pgrep_calls == [f"-f {HERMES_CRON_PATTERN}"]


def test_module_failure_code_is_propagated_with_nothing_printed_after(tmp_path):
    proc, _calls, _pgrep, _project = _run(tmp_path, exit_code=3)

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 3
    assert lines[-1] == "counters examined=3"
    assert "CONSOLE_RESULT" not in proc.stdout + proc.stderr


def test_refuses_to_start_while_the_hermes_cron_runs(tmp_path):
    proc, calls, pgrep_calls, _project = _run(tmp_path, hermes_running=True)

    assert proc.returncode == 5
    assert "console: hermes_cron_running" in proc.stdout
    assert calls == []
    assert "CONSOLE_RESULT" not in proc.stdout + proc.stderr
    assert pgrep_calls == [f"-f {HERMES_CRON_PATTERN}"]


def test_script_defaults_are_the_absolute_production_paths():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'PROJECT="${PROJECT:-/opt/repos/hermes-social/social/tools/meta-webhook}"' in text
    assert 'META_ENV_FILE="${META_ENV_FILE:-/opt/data/meta-comment-dm-automation/.env}"' in text
    assert 'META_DB="${META_DB:-/opt/data/meta-comment-dm-automation/data/processed_comments.sqlite3}"' in text
    assert 'UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/meta-webhook}"' in text
    assert "set -euo pipefail" in text
    assert "cd /opt/data" not in text
    assert "docker" not in text
