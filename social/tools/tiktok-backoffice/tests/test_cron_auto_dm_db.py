"""s13 — the cron script waits 30 s for a lock (console ADR 037 §3)."""

import importlib.util
import sqlite3
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tiktok_cron_auto_dm.py"


def _load():
    spec = importlib.util.spec_from_file_location("tiktok_cron_auto_dm_s13", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_db_helpers_pass_timeout_30(tmp_path, monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "DB", tmp_path / "tiktok.sqlite3")
    seen = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", spy)

    module.db_exec("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    module.db_rows("SELECT COUNT(*) AS n FROM t")

    assert seen == [30, 30]
