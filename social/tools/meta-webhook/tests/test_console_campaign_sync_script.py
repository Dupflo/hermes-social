"""s14-campaign-sync-job — wrapper run by the hermes-console worker (job `campaign-sync`, console ADR 011/040).

The wrapper must run the repo's `app.console_campaign_sync` from the meta-webhook project with five
absolute paths, print a module proof line first, leave the module's output as the last line and
propagate its exit code. AC 1: it is the only caller of the kanban syncs in this project.
"""

import os
import re
import subprocess
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "console-campaign-sync.sh"
RESULT_LINE = 'CONSOLE_RESULT {"status":"ok","counters":{"meta_created":1}}'

FAKE_UV = """#!/usr/bin/env bash
echo "$PWD|$*" >> "$FAKE_UV_LOG"
case "$*" in
  *" -c "*) echo "console: module=$PWD/app/console_campaign_sync.py" ;;
  *" -m app.console_campaign_sync"*)
    echo "console: kanban_db=x meta_db=y tiktok_db=z"
    echo "meta: ok created=1 updated=0 skipped_console=0 disabled=0"
    if [ "${FAKE_UV_EXIT:-0}" != "0" ]; then
      echo "counters meta_created=1"
      exit "$FAKE_UV_EXIT"
    fi
    echo '""" + RESULT_LINE + """' ;;
esac
"""

PATHS = {
    "KANBAN_DB": "/abs/kanban/boards/meta-campaigns/kanban.db",
    "META_DB": "/abs/meta/data/processed_comments.sqlite3",
    "TIKTOK_DB": "/abs/tiktok/tiktok_backoffice.sqlite3",
    "TIKTOK_PROJECT": "/abs/repos/tiktok-backoffice",
    "TIKTOK_VENV": "/abs/console-venvs/tiktok-backoffice",
}
ARGS = {
    "--kanban-db": "KANBAN_DB",
    "--meta-db": "META_DB",
    "--tiktok-db": "TIKTOK_DB",
    "--tiktok-project": "TIKTOK_PROJECT",
    "--tiktok-venv": "TIKTOK_VENV",
}


def _run(tmp_path: Path, *, exit_code: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(FAKE_UV, encoding="utf-8")
    fake.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    uv_log = tmp_path / "uv.log"
    env = {
        **os.environ,
        **PATHS,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT": str(project),
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        "FAKE_UV_LOG": str(uv_log),
        "FAKE_UV_EXIT": str(exit_code),
    }
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    calls = uv_log.read_text(encoding="utf-8").splitlines() if uv_log.exists() else []
    return proc, calls, project


def test_runs_the_module_from_the_project_with_absolute_paths_and_ends_with_its_output(tmp_path):
    proc, calls, project = _run(tmp_path)

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 0, proc.stderr
    assert lines[-1] == RESULT_LINE
    module_idx = next(i for i, line in enumerate(lines) if line.startswith("console: module="))
    assert module_idx < lines.index("console: kanban_db=x meta_db=y tiktok_db=z")
    assert len(calls) == 2
    assert all(call.split("|", 1)[0] == str(project) for call in calls)
    args = calls[1].split("|", 1)[1].split()
    assert args[:6] == ["run", "--frozen", "--no-dev", "python", "-m", "app.console_campaign_sync"]
    for flag, variable in ARGS.items():
        value = args[args.index(flag) + 1]
        assert value == PATHS[variable]
        assert Path(value).is_absolute()


def test_module_failure_code_is_propagated_with_nothing_printed_after(tmp_path):
    proc, _calls, _project = _run(tmp_path, exit_code=6)

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 6
    assert lines[-1] == "counters meta_created=1"
    assert "CONSOLE_RESULT" not in proc.stdout + proc.stderr


def test_script_defaults_are_the_absolute_production_paths():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'PROJECT="${PROJECT:-/opt/repos/hermes-social/social/tools/meta-webhook}"' in text
    assert 'TIKTOK_PROJECT="${TIKTOK_PROJECT:-/opt/repos/hermes-social/social/tools/tiktok-backoffice}"' in text
    assert 'KANBAN_DB="${KANBAN_DB:-/opt/data/kanban/boards/meta-campaigns/kanban.db}"' in text
    assert 'META_DB="${META_DB:-/opt/data/meta-comment-dm-automation/data/processed_comments.sqlite3}"' in text
    assert 'TIKTOK_DB="${TIKTOK_DB:-/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3}"' in text
    assert 'TIKTOK_VENV="${TIKTOK_VENV:-/opt/data/console-venvs/tiktok-backoffice}"' in text
    assert 'UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/meta-webhook}"' in text
    assert "set -euo pipefail" in text
    assert "pgrep" not in text
    assert "docker" not in text


def test_the_wrapper_is_the_only_caller_of_the_kanban_syncs():
    pattern = re.compile(r"console_campaign_sync|kanban_sync|sync-kanban-campaigns")
    callers = sorted(path.name for path in (PROJECT_DIR / "scripts").glob("*.sh") if pattern.search(path.read_text(encoding="utf-8")))
    assert callers == ["console-campaign-sync.sh"]

    invokers = sorted(
        path.name for path in (PROJECT_DIR / "app").glob("*.py") if "sync-kanban-campaigns" in path.read_text(encoding="utf-8")
    )
    assert invokers == ["console_campaign_sync.py"]
