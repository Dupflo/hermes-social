"""s14-campaign-sync-job — entry point of the hermes-console job `campaign-sync` (console ADR 040).

Real Meta and kanban databases in tmp_path; TikTok is a fake `uv` executable that logs how it was
called and answers according to FAKE_UV_MODE. The module must run Meta in process, then TikTok in a
subprocess, isolate each side, map failures to exit codes 6-10 and never print card content,
exception messages or the TikTok stderr.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app import console_campaign_sync as module
from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.console_campaign_sync import SyncConfig, main, run_campaign_sync

SECRET_DM = "Texte DM secret de la carte"
SECRET_STDERR = "contenu de carte privee https://prive.example.com/x"
COUNTER_KEYS = [
    "meta_created",
    "meta_updated",
    "meta_skipped_console",
    "meta_disabled",
    "tiktok_created",
    "tiktok_updated",
    "tiktok_skipped_console",
]

CARD = """
Statut métier: Actif
Plateforme: {platform}
Mots-clés: {keyword}
Réponse publique: C'est envoyé
Message DM: """ + SECRET_DM + """
"""

FAKE_UV = """#!/usr/bin/env bash
{
  echo "argv=$*"
  echo "cwd=$PWD"
  echo "uv_project_environment=${UV_PROJECT_ENVIRONMENT:-}"
  if [ -n "${VIRTUAL_ENV+x}" ]; then echo "virtual_env=present"; else echo "virtual_env=absent"; fi
} >> "$FAKE_UV_LOG"
case "${FAKE_UV_MODE:-ok}" in
  ok)
    echo 'progress line'
    echo '{"ok": true, "synced": 5, "created": 2, "updated": 3, "skipped_console": 4}' ;;
  fail)
    echo 'Traceback (most recent call last):' >&2
    echo '  File "app/store.py", line 400, in upsert_campaign' >&2
    echo "ValueError: $FAKE_UV_SECRET" >&2
    exit 1 ;;
  fail_plain)
    echo "error: $FAKE_UV_SECRET" >&2
    exit 2 ;;
  garbage)
    echo 'not json at all' ;;
  no_ok)
    echo '{"synced": 1, "created": 1, "updated": 0, "skipped_console": 0}' ;;
  bad_counter)
    echo '{"ok": true, "created": -1, "updated": 0, "skipped_console": 0}' ;;
  hang)
    echo $$ > "$FAKE_UV_PIDS"
    sleep 60 &
    echo $! >> "$FAKE_UV_PIDS"
    sleep 60 ;;
esac
"""


def _kanban(path: Path, cards: list[tuple[str, str, str]], *, with_tasks: bool = True) -> Path:
    with sqlite3.connect(path) as connection:
        if with_tasks:
            connection.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, status TEXT NOT NULL)"
            )
            for task_id, title, body in cards:
                connection.execute(
                    "INSERT INTO tasks (id, title, body, status) VALUES (?, ?, ?, 'ready')", (task_id, title, body)
                )
        else:
            connection.execute("CREATE TABLE other (id TEXT)")
    return path


def _console_row(db_path: Path, task_id: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO campaign_rules (
                source_task_id, name, platform, media_id, keywords_json, public_reply_text, dm_text,
                enabled, delivery_mode, source, created_at, updated_at
            ) VALUES (?, 'Console', 'instagram', NULL, '["voicebox"]', 'R', 'D', 1,
                      'follow_gate', 'console', '2026-10-01 10:00:00', '2026-10-02 10:00:00')
            """,
            (task_id,),
        )


def _rules(db_path: Path) -> dict[str, tuple]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT * FROM campaign_rules ORDER BY source_task_id").fetchall()
    return {row[0]: row for row in rows}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Meta db with 1 console row + 1 active kanban rule absent from the kanban (=> disabled 1);
    kanban with the console card (=> skipped_console 1) and a new Instagram card (=> created 1)."""
    meta_db = tmp_path / "meta" / "processed_comments.sqlite3"
    meta_db.parent.mkdir()
    store = CampaignRuleStore(meta_db)
    _console_row(meta_db, "task-console")
    store.upsert_rule(
        CampaignRule(
            source_task_id="task-gone", name="Gone", platform="any", media_id=None,
            keywords=["gone"], public_reply_text="R", dm_text="D", enabled=True,
        )
    )
    kanban_db = _kanban(
        tmp_path / "kanban.db",
        [
            ("task-console", "Same id", CARD.format(platform="facebook", keyword="other")),
            ("task-new", "New", CARD.format(platform="instagram", keyword="proxy")),
        ],
    )
    tiktok_db = tmp_path / "tiktok" / "tiktok_backoffice.sqlite3"
    tiktok_db.parent.mkdir()
    tiktok_db.write_bytes(b"")
    tiktok_project = tmp_path / "tiktok-backoffice"
    tiktok_project.mkdir()
    fake_uv = tmp_path / "bin" / "uv"
    fake_uv.parent.mkdir()
    fake_uv.write_text(FAKE_UV, encoding="utf-8")
    fake_uv.chmod(0o755)
    log = tmp_path / "uv.log"
    pids = tmp_path / "uv.pids"
    monkeypatch.setenv("FAKE_UV_LOG", str(log))
    monkeypatch.setenv("FAKE_UV_PIDS", str(pids))
    monkeypatch.setenv("FAKE_UV_SECRET", SECRET_STDERR)
    monkeypatch.setenv("FAKE_UV_MODE", "ok")
    # Inherited from the Meta `uv run`: must never leak to the TikTok child.
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "meta-venv"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "meta-venv"))
    config = SyncConfig(
        kanban_db=kanban_db,
        meta_db=meta_db,
        tiktok_db=tiktok_db,
        tiktok_project=tiktok_project,
        tiktok_venv=tmp_path / "console-venvs" / "tiktok-backoffice",
    )

    class Env:
        pass

    e = Env()
    e.tmp_path, e.config, e.uv, e.log, e.pids = tmp_path, config, str(fake_uv), log, pids
    return e


def _calls(e) -> list[dict[str, str]]:
    if not e.log.exists():
        return []
    calls: list[dict[str, str]] = []
    for line in e.log.read_text(encoding="utf-8").splitlines():
        key, value = line.split("=", 1)
        if key == "argv":
            calls.append({})
        calls[-1][key] = value
    return calls


def _assert_no_leak(outcome) -> None:
    text = "\n".join(outcome.lines)
    assert SECRET_DM not in text
    assert SECRET_STDERR not in text
    assert "no such table" not in text
    assert "readonly" not in text
    assert "Traceback" not in text
    assert '"empty"' not in text and '"skipped"' not in text


def test_nominal_runs_meta_then_tiktok_and_ends_with_console_result(env):
    outcome = run_campaign_sync(env.config, uv=env.uv)

    assert outcome.exit_code == 0
    c = env.config
    assert outcome.lines[0] == f"console: kanban_db={c.kanban_db} meta_db={c.meta_db} tiktok_db={c.tiktok_db}"
    assert outcome.lines[1] == "meta: ok created=1 updated=0 skipped_console=1 disabled=1"
    assert outcome.lines[2] == "tiktok: ok created=2 updated=3 skipped_console=4"
    assert outcome.lines[3] == (
        "counters meta_created=1 meta_updated=0 meta_skipped_console=1 meta_disabled=1 "
        "tiktok_created=2 tiktok_updated=3 tiktok_skipped_console=4"
    )
    last = outcome.lines[-1]
    assert len(outcome.lines) == 5
    assert last.startswith("CONSOLE_RESULT ")
    result = json.loads(last[len("CONSOLE_RESULT "):])
    assert result["status"] == "ok"
    assert list(result["counters"]) == COUNTER_KEYS
    assert result["counters"] == {
        "meta_created": 1,
        "meta_updated": 0,
        "meta_skipped_console": 1,
        "meta_disabled": 1,
        "tiktok_created": 2,
        "tiktok_updated": 3,
        "tiktok_skipped_console": 4,
    }
    assert outcome.counters == result["counters"]
    rules = _rules(c.meta_db)
    assert rules["task-new"][7] == 1
    assert rules["task-gone"][7] == 0
    calls = _calls(env)
    assert len(calls) == 1
    assert calls[0]["argv"] == (
        f"run --frozen --no-dev tiktok-backoffice --db {c.tiktok_db} sync-kanban-campaigns --kanban-db {c.kanban_db}"
    )
    assert Path(calls[0]["cwd"]).resolve() == c.tiktok_project.resolve()
    assert calls[0]["uv_project_environment"] == str(c.tiktok_venv)
    assert calls[0]["virtual_env"] == "absent"
    _assert_no_leak(outcome)


def test_meta_failure_still_runs_tiktok_and_exits_6(env, tmp_path):
    kanban_without_tasks = _kanban(tmp_path / "broken-kanban.db", [], with_tasks=False)
    config = SyncConfig(**{**env.config.__dict__, "kanban_db": kanban_without_tasks})

    outcome = run_campaign_sync(config, uv=env.uv)

    assert outcome.exit_code == 6
    assert "meta: failed OperationalError" in outcome.lines
    assert "tiktok: ok created=2 updated=3 skipped_console=4" in outcome.lines
    assert outcome.lines[-1] == "counters tiktok_created=2 tiktok_updated=3 tiktok_skipped_console=4"
    assert not any("CONSOLE_RESULT" in line for line in outcome.lines)
    assert len(_calls(env)) == 1
    _assert_no_leak(outcome)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
def test_meta_failure_on_a_read_only_meta_db_exits_6(env):
    env.config.meta_db.chmod(0o444)
    env.config.meta_db.parent.chmod(0o555)
    try:
        outcome = run_campaign_sync(env.config, uv=env.uv)
    finally:
        env.config.meta_db.parent.chmod(0o755)
        env.config.meta_db.chmod(0o644)

    assert outcome.exit_code == 6
    assert "meta: failed OperationalError" in outcome.lines
    assert len(_calls(env)) == 1
    assert not any("CONSOLE_RESULT" in line for line in outcome.lines)
    _assert_no_leak(outcome)


def test_tiktok_failure_keeps_meta_written_and_exits_7_without_leaking_stderr(env, monkeypatch):
    monkeypatch.setenv("FAKE_UV_MODE", "fail")

    outcome = run_campaign_sync(env.config, uv=env.uv)

    assert outcome.exit_code == 7
    assert "meta: ok created=1 updated=0 skipped_console=1 disabled=1" in outcome.lines
    assert "tiktok: failed exit=1 ValueError" in outcome.lines
    assert outcome.lines[-1] == "counters meta_created=1 meta_updated=0 meta_skipped_console=1 meta_disabled=1"
    assert not any("CONSOLE_RESULT" in line for line in outcome.lines)
    assert _rules(env.config.meta_db)["task-new"][7] == 1
    _assert_no_leak(outcome)


def test_tiktok_failure_without_a_recognisable_exception_names_only_the_exit_code(env, monkeypatch):
    monkeypatch.setenv("FAKE_UV_MODE", "fail_plain")

    outcome = run_campaign_sync(env.config, uv=env.uv)

    assert outcome.exit_code == 7
    assert "tiktok: failed exit=2" in outcome.lines
    _assert_no_leak(outcome)


def test_tiktok_timeout_kills_the_whole_process_group(env, monkeypatch):
    monkeypatch.setenv("FAKE_UV_MODE", "hang")

    started = time.monotonic()
    outcome = run_campaign_sync(env.config, uv=env.uv, tiktok_timeout=1)

    assert time.monotonic() - started < 30
    assert outcome.exit_code == 7
    assert "tiktok: failed timeout" in outcome.lines
    pids = [int(pid) for pid in env.pids.read_text(encoding="utf-8").split()]
    assert len(pids) == 2
    deadline = time.monotonic() + 5
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        still = []
        for pid in alive:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            still.append(pid)
        alive = still
        if alive:
            time.sleep(0.1)
    assert alive == []
    _assert_no_leak(outcome)


def test_default_tiktok_timeout_is_100_seconds_minus_elapsed_with_a_floor_of_10(monkeypatch):
    clock = iter([1000.0, 1030.0, 2000.0, 2095.0, 3000.0, 3000.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))

    assert module._default_tiktok_timeout(module.time.monotonic()) == 70
    assert module._default_tiktok_timeout(module.time.monotonic()) == 10
    assert module._default_tiktok_timeout(module.time.monotonic()) == 100


@pytest.mark.parametrize("mode", ["garbage", "no_ok", "bad_counter"])
def test_tiktok_unreadable_output_is_invalid_output_and_exits_7(env, monkeypatch, mode):
    monkeypatch.setenv("FAKE_UV_MODE", mode)

    outcome = run_campaign_sync(env.config, uv=env.uv)

    assert outcome.exit_code == 7
    assert "tiktok: failed exit=0 invalid_output" in outcome.lines
    assert not any("CONSOLE_RESULT" in line for line in outcome.lines)
    _assert_no_leak(outcome)


def test_both_sides_failing_exits_8_without_counters(env, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_UV_MODE", "fail")
    kanban_without_tasks = _kanban(tmp_path / "broken-kanban.db", [], with_tasks=False)
    config = SyncConfig(**{**env.config.__dict__, "kanban_db": kanban_without_tasks})

    outcome = run_campaign_sync(config, uv=env.uv)

    assert outcome.exit_code == 8
    assert "meta: failed OperationalError" in outcome.lines
    assert "tiktok: failed exit=1 ValueError" in outcome.lines
    assert not any(line.startswith("counters") for line in outcome.lines)
    assert not any("CONSOLE_RESULT" in line for line in outcome.lines)
    _assert_no_leak(outcome)


def test_missing_kanban_exits_9_and_runs_nothing(env, tmp_path):
    before = _rules(env.config.meta_db)
    config = SyncConfig(**{**env.config.__dict__, "kanban_db": tmp_path / "absent" / "kanban.db"})

    outcome = run_campaign_sync(config, uv=env.uv)

    assert outcome.exit_code == 9
    assert outcome.lines[-1] == "console: kanban_missing"
    assert _calls(env) == []
    assert _rules(env.config.meta_db) == before
    _assert_no_leak(outcome)


@pytest.mark.parametrize("param", ["kanban_db", "meta_db", "tiktok_db", "tiktok_project", "tiktok_venv"])
def test_relative_path_is_invalid_config_and_exits_10(env, param):
    config = SyncConfig(**{**env.config.__dict__, param: Path("relative") / "path"})

    outcome = run_campaign_sync(config, uv=env.uv)

    assert outcome.exit_code == 10
    assert outcome.lines == [f"console: invalid_config {param}"]
    assert _calls(env) == []


@pytest.mark.parametrize("param", ["meta_db", "tiktok_db"])
def test_missing_database_is_invalid_config_and_creates_no_file(env, tmp_path, param):
    missing = tmp_path / "nowhere" / "missing.sqlite3"
    config = SyncConfig(**{**env.config.__dict__, param: missing})

    outcome = run_campaign_sync(config, uv=env.uv)

    assert outcome.exit_code == 10
    assert outcome.lines == [f"console: invalid_config {param}"]
    assert not missing.exists()
    assert not missing.parent.exists()
    assert _calls(env) == []


def test_tiktok_project_must_be_a_directory(env, tmp_path):
    config = SyncConfig(**{**env.config.__dict__, "tiktok_project": env.config.tiktok_db})

    outcome = run_campaign_sync(config, uv=env.uv)

    assert outcome.exit_code == 10
    assert outcome.lines == ["console: invalid_config tiktok_project"]
    assert _calls(env) == []


def test_main_parses_arguments_prints_lines_and_returns_the_exit_code(env, monkeypatch, capsys):
    monkeypatch.setattr(module, "UV", env.uv)
    c = env.config

    code = main(
        [
            "--kanban-db", str(c.kanban_db),
            "--meta-db", str(c.meta_db),
            "--tiktok-db", str(c.tiktok_db),
            "--tiktok-project", str(c.tiktok_project),
            "--tiktok-venv", str(c.tiktok_venv),
        ]
    )

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert code == 0
    assert lines[0].startswith("console: kanban_db=")
    assert lines[-1].startswith('CONSOLE_RESULT {"status":"ok","counters":{')
    assert SECRET_DM not in out


def test_main_requires_all_five_arguments(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--kanban-db", "/abs/kanban.db"])

    assert excinfo.value.code == 2
