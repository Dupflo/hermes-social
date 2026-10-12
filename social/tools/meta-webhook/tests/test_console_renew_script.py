"""s04-meta-token-renewal — script run by the hermes-console worker (ADR 011/022).

The script must run the repo's `app.token_renewer` (never the frozen copy in
/opt/data/meta-comment-dm-automation), restart nothing, and end with the
`CONSOLE_RESULT` line on success only.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_DIR / "scripts" / "console-renew-meta-tokens.sh"
RESULT_LINE = 'CONSOLE_RESULT {"status":"ok","reason":"tokens_renewed"}'

FAKE_UV = """#!/usr/bin/env bash
echo "$PWD|$*" >> "$FAKE_UV_LOG"
case "$*" in
  *" -c "*) echo "console: module=$PWD/app/token_renewer.py" ;;
  *" -m app.token_renewer"*)
    if [ "${FAKE_UV_EXIT:-0}" != "0" ]; then
      echo "Meta token renewal failed: network error (ConnectError)"
      exit "$FAKE_UV_EXIT"
    fi
    echo "Meta tokens renewed and .env updated" ;;
esac
"""


def _run(tmp_path: Path, exit_code: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(FAKE_UV, encoding="utf-8")
    fake.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    log = tmp_path / "uv.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT": str(project),
        "META_ENV_FILE": "/abs/meta/.env",
        "UV_PROJECT_ENVIRONMENT": str(tmp_path / "venv"),
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_EXIT": str(exit_code),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls, project


def test_success_prints_module_then_renews_then_ends_with_console_result(tmp_path):
    proc, calls, project = _run(tmp_path)

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 0, proc.stderr
    assert lines[-1] == RESULT_LINE
    assert "tokens renouvelés" in lines
    module_idx = next(i for i, line in enumerate(lines) if line.startswith("console: module="))
    renewed_idx = lines.index("Meta tokens renewed and .env updated")
    assert module_idx < renewed_idx
    assert len(calls) == 2
    cwd, args = calls[1].split("|", 1)
    assert cwd == str(project)
    assert "--env-file /abs/meta/.env" in args
    assert "--no-deploy-trigger" in args
    assert all(call.split("|", 1)[0] == str(project) for call in calls)


def test_failure_exits_non_zero_without_console_result(tmp_path):
    proc, _calls, _project = _run(tmp_path, exit_code=1)

    assert proc.returncode != 0
    assert "CONSOLE_RESULT" not in proc.stdout + proc.stderr
    assert "tokens renouvelés" not in proc.stdout


def test_script_never_uses_the_frozen_copy_nor_restarts_anything():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "meta-comment-dm-automation &&" not in text
    assert "cd /opt/data" not in text
    assert "docker" not in text
    assert "--deploy-trigger " not in text.replace("--no-deploy-trigger", "")
    assert "--no-deploy-trigger" in text


def test_module_resolves_to_the_repo_file_from_the_project_dir():
    out = subprocess.run(
        [sys.executable, "-c", "import app.token_renewer as m; print(m.__file__)"],
        cwd=PROJECT_DIR,
        capture_output=True,
        text=True,
        timeout=30,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()) == PROJECT_DIR / "app" / "token_renewer.py"
