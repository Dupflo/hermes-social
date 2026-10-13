import logging
import sqlite3

from fastapi.testclient import TestClient


PRE_S13_DDL = """
CREATE TABLE campaign_rules (
    source_task_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    media_id TEXT,
    keywords_json TEXT NOT NULL,
    public_reply_text TEXT NOT NULL,
    dm_text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    delivery_mode TEXT NOT NULL DEFAULT 'direct',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _columns(db):
    with sqlite3.connect(db) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(campaign_rules)")]


def test_lifespan_adds_source_column(tmp_path, monkeypatch):
    db = tmp_path / "processed_comments.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute(PRE_S13_DDL)
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(db))

    import app.main

    with TestClient(app.main.app):
        pass

    assert "source" in _columns(db)


def test_lifespan_survives_an_unusable_database(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(blocker / "processed_comments.sqlite3"))

    import app.main

    with caplog.at_level(logging.WARNING):
        with TestClient(app.main.app) as client:
            assert client.get("/health").status_code == 200

    assert "campaign_rules_schema_failed" in caplog.text
