import sqlite3

from app.store import ProcessedCommentStore


def test_store_marks_comment_as_processed(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")

    assert store.was_processed("instagram", "comment-1") is False

    store.mark_processed(
        platform="instagram",
        comment_id="comment-1",
        keyword="proxy",
        like_sent=True,
        public_reply_sent=True,
        dm_sent=True,
    )

    assert store.was_processed("instagram", "comment-1") is True


def test_store_is_idempotent_for_same_comment(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")

    first = store.try_claim("instagram", "comment-1", "proxy")
    second = store.try_claim("instagram", "comment-1", "proxy")

    assert first is True
    assert second is False
    assert store.was_processed("instagram", "comment-1") is True


def test_mark_failed_stores_error_message(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")

    store.mark_failed(
        platform="facebook",
        comment_id="comment-1",
        keyword="proxy",
        error_message="Meta Graph API error 100: unsupported",
    )

    assert store.recent_failures(limit=1) == [
        {
            "platform": "facebook",
            "comment_id": "comment-1",
            "keyword": "proxy",
            "error_message": "Meta Graph API error 100: unsupported",
        }
    ]


def test_comment_status_returns_current_status(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")

    assert store.comment_status("facebook", "comment-1") is None

    store.mark_failed(platform="facebook", comment_id="comment-1", keyword="proxy")

    assert store.comment_status("facebook", "comment-1") == "failed"


def test_store_reports_fully_processed_only_when_public_reply_and_dm_are_sent(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")

    store.mark_processed(
        platform="facebook",
        comment_id="partial-comment",
        keyword="proxy",
        like_sent=False,
        public_reply_sent=False,
        dm_sent=True,
    )

    assert store.delivery_state("facebook", "partial-comment") == {
        "status": "processed",
        "like_sent": False,
        "public_reply_sent": False,
        "dm_sent": True,
    }
    assert store.is_fully_processed("facebook", "partial-comment") is False
    assert store.try_claim("facebook", "partial-comment", "proxy") is True

    store.mark_processed(
        platform="facebook",
        comment_id="partial-comment",
        keyword="proxy",
        like_sent=True,
        public_reply_sent=True,
        dm_sent=False,
    )

    assert store.delivery_state("facebook", "partial-comment") == {
        "status": "processed",
        "like_sent": True,
        "public_reply_sent": True,
        "dm_sent": True,
    }
    assert store.is_fully_processed("facebook", "partial-comment") is True


def test_store_allows_retrying_stale_claimed_comment(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")

    assert store.try_claim("facebook", "stale-claim", "proxy") is True
    assert store.try_claim("facebook", "stale-claim", "proxy") is False

    with store._connect() as connection:
        connection.execute(
            "UPDATE processed_comments SET created_at = datetime('now', '-10 minutes') WHERE comment_id = ?",
            ("stale-claim",),
        )

    assert store.try_claim("facebook", "stale-claim", "proxy") is True


def test_store_migrates_existing_database_with_delivery_columns(tmp_path):
    database = tmp_path / "processed.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE processed_comments (
                platform TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                PRIMARY KEY (platform, comment_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO processed_comments (platform, comment_id, keyword, status) VALUES (?, ?, ?, ?)",
            ("facebook", "legacy-comment", "proxy", "processed"),
        )

    store = ProcessedCommentStore(database)

    assert store.delivery_state("facebook", "legacy-comment") == {
        "status": "processed",
        "like_sent": False,
        "public_reply_sent": False,
        "dm_sent": False,
    }
    assert store.try_claim("facebook", "legacy-comment", "proxy") is True


def test_store_stale_reclaim_blocks_immediate_second_reclaim(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")

    assert store.try_claim("facebook", "stale-claim-once", "proxy") is True
    with store._connect() as connection:
        connection.execute(
            "UPDATE processed_comments SET created_at = datetime('now', '-10 minutes') WHERE comment_id = ?",
            ("stale-claim-once",),
        )

    assert store.try_claim("facebook", "stale-claim-once", "proxy") is True
    assert store.try_claim("facebook", "stale-claim-once", "proxy") is False


def test_wrong_resource_sent_is_terminal(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO processed_comments (platform, comment_id, keyword, status, public_reply_sent, dm_sent, error_message)
            VALUES ('instagram', 'comment-1', 'proxy', 'wrong_resource_sent', 1, 0, 'wrong resource sent')
            """
        )

    assert store.is_terminal("instagram", "comment-1") is True
