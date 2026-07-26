import sqlite3

from app.outbound_message_store import OutboundMessageStore


def test_outbound_message_store_records_sent_message_with_meta_response_id(tmp_path):
    store = OutboundMessageStore(tmp_path / "processed.sqlite3")

    message_id = store.record_sent(
        platform="instagram",
        source_type="comment",
        source_id="comment-1",
        recipient_id="comment-1",
        message_type="private_reply",
        message_text="Voici le lien",
        meta_response_id="meta-message-1",
    )

    rows = store.list_for_source("instagram", "comment-1")
    assert len(rows) == 1
    assert rows[0]["id"] == message_id
    assert rows[0]["source_type"] == "comment"
    assert rows[0]["message_type"] == "private_reply"
    assert rows[0]["message_text"] == "Voici le lien"
    assert rows[0]["meta_response_id"] == "meta-message-1"
    assert rows[0]["status"] == "sent"


def test_outbound_message_store_migrates_existing_database(tmp_path):
    database = tmp_path / "processed.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE processed_comments (
                platform TEXT NOT NULL,
                comment_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(platform, comment_id)
            )
            """
        )

    OutboundMessageStore(database)

    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outbound_messages'"
        ).fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outbound_messages)")}

    assert table is not None
    assert {
        "id",
        "platform",
        "source_type",
        "source_id",
        "recipient_id",
        "message_type",
        "message_text",
        "meta_response_id",
        "status",
        "created_at",
    } <= columns
