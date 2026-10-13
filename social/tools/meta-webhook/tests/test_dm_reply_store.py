"""s17 (console ADR 045 §2) — `console_dm_replies`: the idempotency key is taken before any Graph call."""

import sqlite3
import threading

import pytest

from app.comment_review_store import CommentReviewStore
from app.dm_reply_store import DirectReplyStore
from app.outbound_message_store import OutboundMessageStore

KEY = "k" * 16


def _tables(database):
    with sqlite3.connect(database) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _row(database, key=KEY):
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        found = connection.execute("SELECT * FROM console_dm_replies WHERE idempotency_key = ?", (key,)).fetchone()
        return dict(found) if found else None


def _immediate_is_free(database):
    connection = sqlite3.connect(database, timeout=0)
    connection.isolation_level = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def test_schema_is_exactly_the_adr_columns(tmp_path):
    database = tmp_path / "processed.sqlite3"
    DirectReplyStore(database)
    with sqlite3.connect(database) as connection:
        columns = [(row[1], row[2], row[3], row[5]) for row in connection.execute("PRAGMA table_info(console_dm_replies)")]
    assert columns == [
        ("idempotency_key", "TEXT", 0, 1),
        ("platform", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("result", "TEXT", 0, 0),
        ("meta_response_id", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ]


def test_existing_database_only_gains_the_new_table(tmp_path):
    database = tmp_path / "processed.sqlite3"
    OutboundMessageStore(database)
    CommentReviewStore(database)
    with sqlite3.connect(database) as connection:
        before = sorted(connection.execute("SELECT type, name, sql FROM sqlite_master").fetchall(), key=str)
    DirectReplyStore(database)
    DirectReplyStore(database)
    with sqlite3.connect(database) as connection:
        after = sorted(connection.execute("SELECT type, name, sql FROM sqlite_master").fetchall(), key=str)
    added = sorted(entry[:2] for entry in after if entry not in before)
    # The PRIMARY KEY's automatic index belongs to the new table; nothing else is created.
    assert added == [("index", "sqlite_autoindex_console_dm_replies_1"), ("table", "console_dm_replies")]
    assert all(entry in after for entry in before)


def test_first_claim_wins_and_second_loses(tmp_path):
    store = DirectReplyStore(tmp_path / "processed.sqlite3")
    assert store.claim(KEY, "messenger") is True
    assert store.claim(KEY, "messenger") is False
    assert store.claim(KEY, "instagram") is False
    row = _row(tmp_path / "processed.sqlite3")
    assert row["status"] == "sending"
    assert row["platform"] == "messenger"
    assert row["result"] is None


@pytest.mark.parametrize("status", ["sent", "failed", "uncertain"])
def test_a_finished_key_is_never_claimable_again(tmp_path, status):
    store = DirectReplyStore(tmp_path / "processed.sqlite3")
    store.claim(KEY, "messenger")
    assert store.finish(KEY, status, status) is True
    assert store.claim(KEY, "messenger") is False
    assert store.status_of(KEY) == status


def test_two_concurrent_connections_claim_once(tmp_path):
    database = tmp_path / "processed.sqlite3"
    DirectReplyStore(database)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        store = DirectReplyStore(database)
        barrier.wait()
        results.append(store.claim(KEY, "messenger"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]


def test_finish_only_touches_a_sending_key(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = DirectReplyStore(database)
    assert store.finish(KEY, "sent", "sent") is False
    store.claim(KEY, "messenger")
    assert store.finish(KEY, "sent", "sent", meta_response_id="m_1") is True
    assert store.finish(KEY, "failed", "graph_error") is False
    row = _row(database)
    assert row["status"] == "sent"
    assert row["result"] == "sent"
    assert row["meta_response_id"] == "m_1"


def test_status_of_unknown_key_is_none(tmp_path):
    assert DirectReplyStore(tmp_path / "processed.sqlite3").status_of(KEY) is None


def test_every_operation_closes_its_connection(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = DirectReplyStore(database)
    assert _immediate_is_free(database)
    store.claim(KEY, "messenger")
    assert _immediate_is_free(database)
    store.status_of(KEY)
    assert _immediate_is_free(database)
    store.finish(KEY, "sent", "sent")
    assert _immediate_is_free(database)


class _FailingInsert:
    """Wraps the real connection; the INSERT of the claim raises after BEGIN IMMEDIATE."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, *args):
        if sql.lstrip().startswith("INSERT"):
            raise sqlite3.OperationalError("disk I/O error")
        return self.connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_claim_rolls_back_and_closes_on_error(tmp_path, monkeypatch):
    database = tmp_path / "processed.sqlite3"
    store = DirectReplyStore(database)
    real = store._immediate
    monkeypatch.setattr(store, "_immediate", lambda: _FailingInsert(real()))
    with pytest.raises(sqlite3.OperationalError):
        store.claim(KEY, "messenger")
    assert _immediate_is_free(database)
    monkeypatch.undo()
    assert store.status_of(KEY) is None
    assert store.claim(KEY, "messenger") is True
