from app.models import ReplyDraft, ReviewStatus, TikTokComment
from app.store import TikTokBackofficeStore


def test_add_comment_and_next_pending(tmp_path):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    changed = store.add_comment(TikTokComment(video_url="https://www.tiktok.com/@dupflodev/video/123", video_id="123", comment_id="c1", author="@alice", text="proxy"))

    assert changed is True
    assert store.next_pending()["comment_id"] == "c1"


def test_save_draft_marks_comment_drafted(tmp_path):
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    store.add_comment(TikTokComment(video_url="https://example.test/video", comment_id="c1", author=None, text="proxy"))

    store.save_draft(ReplyDraft(comment_id="c1", keyword="proxy", reply_text="Réponse brouillon"))

    assert store.get_comment("c1")["status"] == ReviewStatus.DRAFTED
    assert store.get_draft("c1")["reply_text"] == "Réponse brouillon"



# --- s13: timeout, first additive migration ---------------------------------------------

import sqlite3

import app.store as store_module
from s13_helpers import PRE_S13_CAMPAIGN_DDL


def _campaign_columns(db):
    with sqlite3.connect(db) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(tiktok_campaigns)")]


def test_ensure_schema_adds_source_and_dm_text_to_pre_s13_campaigns(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.executescript(PRE_S13_CAMPAIGN_DDL)
        connection.execute("INSERT INTO tiktok_campaigns (slug, name, reply_template) VALUES ('proxy', 'Proxy', 'R')")

    TikTokBackofficeStore(db)
    TikTokBackofficeStore(db)  # idempotent

    columns = _campaign_columns(db)
    assert columns[-2:] == ["source", "dm_text"]
    assert columns.count("source") == 1 and columns.count("dm_text") == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT source, dm_text FROM tiktok_campaigns").fetchall() == [("kanban", None)]


def test_store_connections_wait_30_seconds_for_a_lock(tmp_path, monkeypatch):
    seen = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", spy)
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    store.list_campaigns()

    assert seen and all(timeout == 30 for timeout in seen)
