"""s09 — store methods added for the replay: pre-check, conditional close, batch selection."""

import sqlite3

import pytest

from app.store import ProcessedCommentStore


@pytest.fixture
def store(tmp_path):
    return ProcessedCommentStore(tmp_path / "processed.sqlite3")


def put(store, comment_id, status, age, *, platform="instagram", like=0, public=0, dm=0, processed_age=None, error=None):
    """Insert a row whose created_at is `datetime('now', age)` (e.g. '-6 minutes')."""
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            INSERT INTO processed_comments (
                platform, comment_id, keyword, status, like_sent, public_reply_sent, dm_sent,
                error_message, created_at, processed_at
            ) VALUES (?, ?, 'guide', ?, ?, ?, ?, ?, datetime('now', ?),
                      CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END)
            """,
            (platform, comment_id, status, like, public, dm, error, age, processed_age, processed_age),
        )


def dump(store, comment_id, platform="instagram"):
    with sqlite3.connect(store.database_path) as connection:
        return connection.execute(
            "SELECT * FROM processed_comments WHERE platform = ? AND comment_id = ?",
            (platform, comment_id),
        ).fetchone()


def sql_time(store, expression):
    with sqlite3.connect(store.database_path) as connection:
        return connection.execute(f"SELECT datetime('now', '{expression}')").fetchone()[0]


# --- replay_state (P1) -----------------------------------------------------------------


def test_replay_state_absent_row_is_none(store):
    assert store.replay_state("instagram", "nope") is None


@pytest.mark.parametrize(
    ("status", "age", "kwargs", "replayable"),
    [
        ("failed", "-1 minutes", {}, True),
        ("claimed", "-4 minutes", {}, False),
        ("claimed", "-6 minutes", {}, True),
        ("processed", "-1 days", {"public": 1, "dm": 1, "processed_age": "-1 days"}, False),
        ("processed", "-1 days", {"public": 1, "dm": 0, "processed_age": "-1 days"}, False),
        ("private_reply_blocked", "-1 days", {}, False),
        ("follow_gate_pending", "-1 days", {}, False),
    ],
)
def test_replay_state_replayable(store, status, age, kwargs, replayable):
    put(store, "c1", status, age, **kwargs)
    state = store.replay_state("instagram", "c1")
    assert state == {"status": status, "replayable": replayable, "window_expired_by_db": False}


def test_replay_state_window_by_db_is_strict_seven_days(store):
    put(store, "old", "failed", "-7 days", )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE processed_comments SET created_at = datetime('now', '-7 days', '-1 minutes') WHERE comment_id = 'old'"
        )
    put(store, "young", "failed", "-6 days")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE processed_comments SET created_at = datetime('now', '-6 days', '-23 hours') WHERE comment_id = 'young'"
        )

    assert store.replay_state("instagram", "old")["window_expired_by_db"] is True
    assert store.replay_state("instagram", "young")["window_expired_by_db"] is False


def test_replay_state_is_per_platform(store):
    put(store, "c1", "failed", "-1 minutes", platform="facebook")
    assert store.replay_state("instagram", "c1") is None
    assert store.replay_state("facebook", "c1")["replayable"] is True


# --- close_manually (P7) ---------------------------------------------------------------


def test_close_manually_closes_a_failed_row_keeping_its_history(store):
    put(store, "c1", "failed", "-2 hours", like=1, public=1, error="GraphAPIError: boom")
    before = dump(store, "c1")

    assert store.close_manually("instagram", "c1", "clos manuellement, fenêtre Meta dépassée") is True

    with sqlite3.connect(store.database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM processed_comments WHERE comment_id = 'c1'").fetchone()
    assert row["status"] == "private_reply_blocked"
    assert row["error_message"] == "clos manuellement, fenêtre Meta dépassée"
    assert row["processed_at"] is not None
    assert row["created_at"] == before[8]
    assert row["like_sent"] == 1
    assert row["public_reply_sent"] == 1
    assert row["dm_sent"] == 0
    assert row["keyword"] == "guide"


def test_close_manually_closes_a_stale_claim(store):
    put(store, "c1", "claimed", "-6 minutes")
    assert store.close_manually("instagram", "c1", "r") is True
    assert store.replay_state("instagram", "c1")["status"] == "private_reply_blocked"


@pytest.mark.parametrize(
    ("status", "age", "kwargs"),
    [
        ("processed", "-1 days", {"public": 1, "dm": 1, "processed_age": "-1 days"}),
        ("claimed", "-4 minutes", {}),
        ("private_reply_blocked", "-1 days", {"error": "blocked"}),
    ],
)
def test_close_manually_refuses_other_statuses_and_leaves_the_row_identical(store, status, age, kwargs):
    put(store, "c1", status, age, **kwargs)
    before = dump(store, "c1")

    assert store.close_manually("instagram", "c1", "r") is False
    assert dump(store, "c1") == before


def test_close_manually_absent_row_is_false_and_creates_nothing(store):
    assert store.close_manually("instagram", "ghost", "r") is False
    assert dump(store, "ghost") is None


# --- batch (P8) ------------------------------------------------------------------------


def keys(rows):
    return [row["comment_id"] for row in rows]


def test_sqlite_now_is_a_sqlite_utc_text(store):
    now = store.sqlite_now()
    assert len(now) == 19 and now[10] == " "


def test_replayable_batch_order_limit_and_exclusions(store):
    put(store, "a", "failed", "-3 hours")
    put(store, "b", "claimed", "-2 hours")
    put(store, "c", "failed", "-1 hours", platform="facebook")
    put(store, "d", "failed", "-1 hours")
    put(store, "recent-claim", "claimed", "-1 minutes")
    put(store, "old", "failed", "-8 days")
    put(store, "done", "processed", "-4 hours", public=1, dm=1, processed_age="-4 hours")
    put(store, "blocked", "private_reply_blocked", "-4 hours")
    put(store, "tiktok", "failed", "-4 hours", platform="tiktok")
    sweep = store.sqlite_now()

    rows = store.replayable_batch(sweep, None, 20)
    assert keys(rows) == ["a", "b", "c", "d"]
    assert rows[0] == {"platform": "instagram", "comment_id": "a", "created_at": sql_time(store, "-3 hours")}
    assert store.count_replayable_after(sweep, None) == 4

    assert keys(store.replayable_batch(sweep, None, 2)) == ["a", "b"]


def test_replayable_batch_excludes_rows_taken_after_the_sweep_started(store):
    put(store, "a", "failed", "-3 hours")
    put(store, "retaken", "failed", "-10 minutes")
    sweep = sql_time(store, "-1 hours")

    assert keys(store.replayable_batch(sweep, None, 20)) == ["a"]
    assert store.count_replayable_after(sweep, None) == 1


def test_replayable_batch_cursor_is_strict(store):
    put(store, "a", "failed", "-3 hours")
    put(store, "b", "failed", "-3 hours")
    put(store, "c", "failed", "-2 hours")
    sweep = store.sqlite_now()
    first = store.replayable_batch(sweep, None, 1)[0]
    after = (first["created_at"], first["platform"], first["comment_id"])

    assert keys(store.replayable_batch(sweep, after, 20)) == ["b", "c"]
    assert store.count_replayable_after(sweep, after) == 2

    last = store.replayable_batch(sweep, after, 20)[-1]
    end = (last["created_at"], last["platform"], last["comment_id"])
    assert store.replayable_batch(sweep, end, 20) == []
    assert store.count_replayable_after(sweep, end) == 0
