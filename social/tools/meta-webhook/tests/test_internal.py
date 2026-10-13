"""s09 — internal listener (:8792): auth, replay, close, batch replay.

The fake Graph client records every call: the 7-day and idempotence guarantees are
assertions on `calls` (no GET, no POST), as in tests/test_processor.py.
"""

import base64
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import app.internal as internal
import app.main as main_module
from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.graph_client import GraphAPIError
from app.store import ProcessedCommentStore


TOKEN = "s09-test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
COMMENT_TEXT = "GUIDE secret-comment-text"
GRAPH_ERROR_TEXT = "secret-graph-error-text"


def graph_date(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeGraph:
    """Records calls. `comments` maps comment_id -> dict returned by get_comment, or an exception."""

    POSTS = {
        "like",
        "public_reply",
        "private_reply",
        "direct_message",
        "facebook_public_reply",
        "facebook_private_reply",
    }

    def __init__(self, comments=None):
        self.calls = []
        self.comments = comments or {}

    def posts(self):
        return [call for call in self.calls if call[0] in self.POSTS]

    async def get_comment(self, comment_id, platform):
        self.calls.append(("get_comment", comment_id, platform))
        value = self.comments.get(comment_id)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            return {
                "text": COMMENT_TEXT,
                "message": COMMENT_TEXT,
                "timestamp": graph_date(timedelta(hours=1)),
                "created_time": graph_date(timedelta(hours=1)),
                "from": {"id": "author-1", "username": "alice", "name": "Alice"},
                "media": {"id": "media-1"},
            }
        return value

    async def like_comment(self, comment_id):
        self.calls.append(("like", comment_id))
        return {"success": True}

    async def reply_to_instagram_comment(self, comment_id, message):
        self.calls.append(("public_reply", comment_id))
        return {"id": "reply-id"}

    async def private_reply_to_instagram_comment(self, page_id, comment_id, text, quick_replies=None):
        self.calls.append(("private_reply", comment_id))
        return {"message_id": "message-id", "recipient_id": "author-1"}

    async def send_direct_message(self, recipient_id, text, quick_replies=None):
        self.calls.append(("direct_message", recipient_id))
        return {"message_id": "dm-id"}

    async def reply_to_facebook_comment(self, comment_id, message):
        self.calls.append(("facebook_public_reply", comment_id))
        return {"id": "reply-id"}

    async def private_reply_to_facebook_comment(self, page_id, comment_id, text):
        self.calls.append(("facebook_private_reply", comment_id))
        return {"id": "message-id"}

    async def can_reply_privately_to_facebook_comment(self, comment_id):
        self.calls.append(("facebook_can_reply_privately", comment_id))
        return True


@pytest.fixture
def env(monkeypatch, tmp_path):
    database = tmp_path / "processed.sqlite3"
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(database))
    monkeypatch.setenv("CONSOLE_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("META_PAGE_ID", "999")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "page-token")
    monkeypatch.setenv("META_OWNER_USERNAMES", "dupflodev")
    monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    graph = FakeGraph()
    monkeypatch.setattr(internal, "_graph_client_factory", lambda settings, http_client: graph)
    store = ProcessedCommentStore(database)
    rules = CampaignRuleStore(database)
    return {"database": database, "graph": graph, "store": store, "rules": rules}


@pytest.fixture
def client():
    return TestClient(internal.app)


def put(env, comment_id, status, age, *, platform="instagram", public=0, dm=0, processed_age=None, error=None):
    with sqlite3.connect(env["database"]) as connection:
        connection.execute(
            """
            INSERT INTO processed_comments (
                platform, comment_id, keyword, status, like_sent, public_reply_sent, dm_sent,
                error_message, created_at, processed_at
            ) VALUES (?, ?, 'guide', ?, 0, ?, ?, ?, datetime('now', ?),
                      CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END)
            """,
            (platform, comment_id, status, public, dm, error, age, processed_age, processed_age),
        )


def row(env, comment_id, platform="instagram"):
    with sqlite3.connect(env["database"]) as connection:
        return connection.execute(
            "SELECT * FROM processed_comments WHERE platform = ? AND comment_id = ?", (platform, comment_id)
        ).fetchone()


def add_rule(env, *, platform="instagram", delivery_mode="direct", source="t1"):
    env["rules"].upsert_rule(
        CampaignRule(
            source_task_id=source,
            name="Guide",
            platform=platform,
            media_id=None,
            keywords=["guide"],
            public_reply_text="C'est envoyé, check tes DM",
            dm_text="Voici le guide : https://example.com/guide",
            delivery_mode=delivery_mode,
        )
    )


def replay(client, comment_id, platform="instagram", headers=AUTH):
    return client.post(f"/internal/comments/{platform}/{comment_id}/replay", headers=headers)


def close(client, comment_id, reason, platform="instagram", headers=AUTH):
    return client.post(f"/internal/comments/{platform}/{comment_id}/close", headers=headers, json={"reason": reason})


def assert_no_meta_data(response):
    body = response.text
    for forbidden in (COMMENT_TEXT, GRAPH_ERROR_TEXT, "access_token", "page-token", "alice", "graph.facebook.com"):
        assert forbidden not in body


ROUTES = [
    ("GET", "/internal/health", None),
    ("POST", "/internal/comments/instagram/111/replay", None),
    ("POST", "/internal/comments/instagram/111/close", {"reason": "window_expired"}),
    ("POST", "/internal/comments/replay-recent", {"cursor": None}),
    ("POST", "/internal/keywords/match", {"text": "x", "keywords": ["x"]}),
    # s15 (console ADR 042): review routes, same auth and same absence from the public app.
    ("POST", "/internal/review/instagram/111/reply", {"text": "x"}),
    ("POST", "/internal/review/instagram/111/skip", None),
    ("POST", "/internal/review/instagram/111/release", None),
]


# --- auth (P5) -------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer wrong-token-of-another-size"},
        {"Authorization": "Bearer " + "x" * len(TOKEN)},
        {"Authorization": f"bearer {TOKEN}x"},
        {"Authorization": TOKEN},
    ],
)
def test_every_route_refuses_a_missing_or_wrong_token(env, client, method, path, body, headers):
    put(env, "111", "failed", "-1 hours")
    response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert env["graph"].calls == []


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_empty_server_token_refuses_everything(env, client, monkeypatch, caplog, method, path, body):
    monkeypatch.setenv("CONSOLE_INTERNAL_TOKEN", "")
    put(env, "111", "failed", "-1 hours")
    with caplog.at_level(logging.WARNING):
        response = client.request(method, path, headers={"Authorization": "Bearer "}, json=body)
        response_2 = client.request(method, path, headers={"Authorization": "Bearer anything"}, json=body)
    assert response.status_code == 401
    assert response_2.status_code == 401
    assert "internal_token_unset" in caplog.text
    assert env["graph"].calls == []


def test_good_token_reaches_health(env, client):
    assert client.get("/internal/health", headers=AUTH).json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_internal_app_has_no_documentation_routes(env, client, path):
    assert client.get(path).status_code == 404
    assert client.get(path, headers=AUTH).status_code == 404


def test_token_never_appears_in_logs(env, client, caplog):
    put(env, "111", "failed", "-1 hours")
    with caplog.at_level(logging.DEBUG):
        client.get("/internal/health", headers={"Authorization": "Bearer nope"})
        client.get("/internal/health", headers=AUTH)
        replay(client, "111")
    assert TOKEN not in caplog.text


# --- not exposed on the public app -----------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_public_app_does_not_serve_internal_routes(env, method, path, body):
    public = TestClient(main_module.app)
    assert public.request(method, path, json=body).status_code == 404
    assert public.request(method, path, headers=AUTH, json=body).status_code == 404
    assert public.get(path, headers=AUTH).status_code == 404


def test_public_app_routes_have_no_internal_prefix():
    assert not [route.path for route in main_module.app.routes if route.path.startswith("/internal")]


# --- > 7 days (AC 3) -------------------------------------------------------------------


def test_older_than_seven_days_by_database_makes_no_graph_call(env, client):
    add_rule(env)
    put(env, "111", "failed", "-8 days")
    before = row(env, "111")

    response = replay(client, "111")

    assert response.status_code == 200
    assert response.json() == {"result": "window_expired", "status": "failed"}
    assert env["graph"].calls == []
    assert row(env, "111") == before


def test_older_than_seven_days_by_graph_date_reads_once_and_sends_nothing(env, client):
    add_rule(env)
    put(env, "111", "failed", "-1 days")
    env["graph"].comments["111"] = {
        "text": COMMENT_TEXT,
        "timestamp": graph_date(timedelta(days=8)),
        "from": {"id": "author-1"},
        "media": {"id": "media-1"},
    }
    before = row(env, "111")

    response = replay(client, "111")

    assert response.json() == {"result": "window_expired", "status": "failed"}
    assert env["graph"].calls == [("get_comment", "111", "instagram")]
    assert env["graph"].posts() == []
    assert row(env, "111") == before


# --- idempotence (AC 4) ----------------------------------------------------------------


def test_fully_processed_comment_is_a_duplicate_without_any_graph_call(env, client):
    add_rule(env)
    put(env, "111", "processed", "-1 hours", public=1, dm=1, processed_age="-1 hours")

    response = replay(client, "111")

    assert response.status_code == 200
    assert response.json() == {"result": "duplicate", "status": "processed"}
    assert env["graph"].calls == []


def test_recent_claim_is_a_duplicate_without_any_graph_call(env, client):
    add_rule(env)
    put(env, "111", "claimed", "-1 minutes")

    assert replay(client, "111").json() == {"result": "duplicate", "status": "claimed"}
    assert env["graph"].calls == []


def test_concurrent_claim_lost_sends_nothing(env, client, monkeypatch):
    add_rule(env)
    put(env, "111", "failed", "-1 hours")
    monkeypatch.setattr(ProcessedCommentStore, "try_claim", lambda *a: False)

    response = replay(client, "111")

    assert response.json() == {"result": "duplicate", "status": "failed"}
    assert env["graph"].calls == [("get_comment", "111", "instagram")]
    assert env["graph"].posts() == []


# --- nominal replay (AC 2) -------------------------------------------------------------


def test_failed_instagram_comment_is_replayed(env, client):
    add_rule(env)
    put(env, "111", "failed", "-1 hours", error=GRAPH_ERROR_TEXT)

    response = replay(client, "111")

    assert response.json() == {"result": "processed", "status": "processed"}
    assert env["graph"].calls == [
        ("get_comment", "111", "instagram"),
        ("private_reply", "111"),
        ("public_reply", "111"),
    ]
    assert_no_meta_data(response)


def test_stale_claim_is_replayed(env, client):
    add_rule(env)
    put(env, "111", "claimed", "-6 minutes")
    assert replay(client, "111").json() == {"result": "processed", "status": "processed"}


def test_failed_facebook_comment_does_not_resend_the_public_reply(env, client):
    add_rule(env, platform="facebook")
    put(env, "123_456", "failed", "-1 hours", platform="facebook", public=1)

    response = replay(client, "123_456", platform="facebook")

    assert response.json() == {"result": "processed", "status": "processed"}
    names = [call[0] for call in env["graph"].calls]
    assert "facebook_private_reply" in names
    assert "facebook_public_reply" not in names


def test_instagram_follow_gate_rule_goes_through_the_gate(env, client, monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "ig-secret")
    add_rule(env, delivery_mode="follow_gate")
    put(env, "111", "failed", "-1 hours")

    response = replay(client, "111")

    assert response.json() == {"result": "follow_gate_pending", "status": "follow_gate_pending"}
    from app.follow_gate_store import FollowGateStore

    assert FollowGateStore(env["database"]).pending_for_sender("instagram", "author-1") is not None


def test_no_active_rule_is_ignored_and_leaves_the_row(env, client):
    add_rule(env, platform="facebook")  # active rules exist, none for this comment
    put(env, "111", "failed", "-1 hours")
    before = row(env, "111")

    response = replay(client, "111")

    assert response.json() == {"result": "ignored", "status": "failed"}
    assert env["graph"].posts() == []
    assert row(env, "111") == before


# --- errors of the GET (P3) ------------------------------------------------------------


@pytest.mark.parametrize(
    ("graph_value", "expected"),
    [
        (GraphAPIError(GRAPH_ERROR_TEXT, code=100), "comment_unavailable"),
        (GraphAPIError(GRAPH_ERROR_TEXT, code=4), "failed_graph_api"),
        (httpx.ConnectError(GRAPH_ERROR_TEXT), "failed_unexpected"),
        ({"text": COMMENT_TEXT, "timestamp": graph_date(timedelta(hours=1)), "media": {"id": "m"}}, "comment_unavailable"),
        ({"text": COMMENT_TEXT, "from": {"id": "author-1"}, "media": {"id": "m"}}, "comment_unavailable"),
        ({"text": COMMENT_TEXT, "timestamp": "hier", "from": {"id": "author-1"}}, "comment_unavailable"),
    ],
)
def test_get_errors_send_nothing_and_leave_the_row(env, client, graph_value, expected):
    add_rule(env)
    put(env, "111", "failed", "-1 hours")
    env["graph"].comments["111"] = graph_value
    before = row(env, "111")

    response = replay(client, "111")

    assert response.status_code == 200
    assert response.json() == {"result": expected, "status": "failed"}
    assert env["graph"].calls == [("get_comment", "111", "instagram")]
    assert row(env, "111") == before
    assert_no_meta_data(response)


def test_unknown_comment_is_404(env, client):
    response = replay(client, "111")
    assert response.status_code == 404
    assert response.json() == {"error": "unknown_comment"}
    assert env["graph"].calls == []


@pytest.mark.parametrize(
    ("platform", "comment_id"),
    [("tiktok", "111"), ("instagram", "abc"), ("instagram", "1_2_3"), ("facebook", "1" * 41)],
)
def test_invalid_platform_or_id_is_422(env, client, platform, comment_id):
    assert replay(client, comment_id, platform=platform).status_code == 422
    assert close(client, comment_id, "ignored", platform=platform).status_code == 422
    assert env["graph"].calls == []


def test_unexpected_error_is_a_bare_500(env, monkeypatch):
    put(env, "111", "failed", "-1 hours")

    def boom(*args, **kwargs):
        raise RuntimeError(GRAPH_ERROR_TEXT)

    monkeypatch.setattr(ProcessedCommentStore, "replay_state", boom)
    response = TestClient(internal.app, raise_server_exceptions=False).post(
        "/internal/comments/instagram/111/replay", headers=AUTH
    )
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}


# --- close (P7) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("window_expired", "clos manuellement, fenêtre Meta dépassée"),
        ("ignored", "clos manuellement, aucune campagne ne correspond"),
        ("comment_unavailable", "clos manuellement, commentaire introuvable chez Meta"),
    ],
)
def test_close_writes_the_fixed_reason(env, client, reason, text):
    put(env, "111", "failed", "-8 days")

    response = close(client, "111", reason)

    assert response.status_code == 200
    assert response.json() == {"result": "closed", "status": "private_reply_blocked"}
    closed = row(env, "111")
    assert closed[3] == "private_reply_blocked"
    assert closed[7] == text
    assert env["graph"].calls == []


def test_close_unknown_reason_is_422(env, client):
    put(env, "111", "failed", "-8 days")
    assert close(client, "111", "because").status_code == 422
    assert row(env, "111")[3] == "failed"


def test_close_processed_is_409(env, client):
    put(env, "111", "processed", "-1 hours", public=1, dm=1, processed_age="-1 hours")
    before = row(env, "111")
    response = close(client, "111", "window_expired")
    assert response.status_code == 409
    assert response.json() == {"error": "not_closable"}
    assert row(env, "111") == before
    assert env["graph"].calls == []


def test_close_unknown_comment_is_404(env, client):
    response = close(client, "111", "window_expired")
    assert response.status_code == 404
    assert response.json() == {"error": "unknown_comment"}


# --- batch (AC 5, P8) ------------------------------------------------------------------


def batch(client, cursor=None):
    return client.post("/internal/comments/replay-recent", headers=AUTH, json={"cursor": cursor})


def seed_batch(env, count):
    for index in range(count):
        # Distinct ages so the order is the insertion order: oldest first.
        put(env, str(1000 + index), "failed", f"-{(count - index) * 10} minutes")


def test_batch_replays_twenty_then_the_rest_with_a_cursor(env, client):
    add_rule(env)
    seed_batch(env, 23)
    put(env, "999", "failed", "-8 days")
    put(env, "998", "processed", "-1 hours", public=1, dm=1, processed_age="-1 hours")

    first = batch(client)
    assert first.status_code == 200
    body = first.json()
    assert body["processed"] == 20
    assert body["remaining"] == 3
    assert isinstance(body["cursor"], str) and body["cursor"]
    assert "=" not in body["cursor"]

    second = batch(client, body["cursor"]).json()
    assert second == {"processed": 3, "blocked": 0, "failed": 0, "skipped": 0, "remaining": 0, "cursor": None}

    touched = {call[1] for call in env["graph"].calls}
    assert "999" not in touched
    assert "998" not in touched
    assert row(env, "999")[3] == "failed"


def test_batch_cursor_skips_rows_left_failed_by_an_ignored_result(env, client, monkeypatch):
    add_rule(env)
    put(env, "1001", "failed", "-30 minutes")
    put(env, "1002", "failed", "-20 minutes")
    env["graph"].comments["1001"] = {
        "text": "no keyword here",
        "timestamp": graph_date(timedelta(hours=1)),
        "from": {"id": "author-1"},
    }
    budget = iter([0, 0, 1000])  # the second row would start past the budget
    monkeypatch.setattr(internal, "_monotonic", lambda: next(budget))
    first = batch(client).json()
    monkeypatch.setattr(internal, "_monotonic", lambda: 0)
    assert first["skipped"] == 1
    assert first["remaining"] == 1
    assert row(env, "1001")[3] == "failed"

    calls_before = len(env["graph"].calls)
    second = batch(client, first["cursor"]).json()
    assert second["processed"] == 1
    assert second["remaining"] == 0
    assert ("get_comment", "1001", "instagram") not in env["graph"].calls[calls_before:]


def test_batch_counts_each_category(env, client):
    add_rule(env)
    put(env, "2001", "failed", "-60 minutes")  # processed
    put(env, "2002", "failed", "-50 minutes")  # failed: comment_unavailable
    put(env, "2003", "failed", "-40 minutes")  # blocked: window_expired by Graph date
    put(env, "2004", "failed", "-30 minutes")  # skipped: ignored (no keyword)
    put(env, "2005", "failed", "-20 minutes")  # failed: failed_graph_api
    graph = env["graph"]
    graph.comments["2002"] = GraphAPIError(GRAPH_ERROR_TEXT, code=100)
    graph.comments["2003"] = {"text": COMMENT_TEXT, "timestamp": graph_date(timedelta(days=9)), "from": {"id": "a"}}
    graph.comments["2004"] = {"text": "rien", "timestamp": graph_date(timedelta(hours=1)), "from": {"id": "a"}}
    graph.comments["2005"] = GraphAPIError(GRAPH_ERROR_TEXT, code=4)

    response = batch(client)

    assert response.json() == {"processed": 1, "blocked": 1, "failed": 2, "skipped": 1, "remaining": 0, "cursor": None}
    assert_no_meta_data(response)


def test_batch_stops_starting_comments_past_the_time_budget(env, client, monkeypatch):
    add_rule(env)
    seed_batch(env, 5)
    clock = iter([0, 10, 50, 95, 96, 97, 98])
    monkeypatch.setattr(internal, "_monotonic", lambda: next(clock))

    body = batch(client).json()

    assert body["processed"] == 2
    assert body["remaining"] == 3
    assert body["cursor"] is not None


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64-%%%",
        base64.urlsafe_b64encode(b"not json").decode().rstrip("="),
        base64.urlsafe_b64encode(json.dumps({"s": 1}).encode()).decode().rstrip("="),
        "",
    ],
)
def test_unreadable_cursor_is_422(env, client, cursor):
    assert batch(client, cursor).status_code == 422
    assert env["graph"].calls == []


def test_batch_with_nothing_to_do(env, client):
    assert batch(client).json() == {
        "processed": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "remaining": 0,
        "cursor": None,
    }


# --- s13: POST /internal/keywords/match (ADR 038 §1) -------------------------------------


def match(client, body, headers=AUTH):
    return client.post("/internal/keywords/match", headers=headers, json=body)


def test_keywords_match_returns_first_matching_index(env, client):
    response = match(client, {"text": "je veux la voicebox", "keywords": ["proxy", "voicebox"]})
    assert response.status_code == 200
    assert response.json() == {"matched": True, "index": 1}


def test_keywords_match_honours_the_meta_negation(env, client):
    response = match(client, {"text": "sans voicebox", "keywords": ["voicebox"]})
    assert response.status_code == 200
    assert response.json() == {"matched": False, "index": None}


def test_keywords_match_several_matches_gives_the_first_index(env, client):
    response = match(client, {"text": "proxy et voicebox", "keywords": ["autre", "voicebox", "proxy"]})
    assert response.json() == {"matched": True, "index": 1}
    assert set(response.json()) == {"matched", "index"}


@pytest.mark.parametrize(
    "body",
    [
        {"text": "x", "keywords": []},
        {"text": "x", "keywords": ["k"] * 101},
        {"text": "x" * 2001, "keywords": ["x"]},
        {"text": "", "keywords": ["x"]},
        {"text": "x", "keywords": [""]},
        {"text": "x", "keywords": ["k" * 101]},
        {"text": "x", "keywords": ["x"], "extra": 1},
        {"keywords": ["x"]},
    ],
)
def test_keywords_match_rejects_invalid_bodies(env, client, body):
    response = match(client, body)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}


def test_keywords_match_touches_neither_graph_nor_the_database(env, client, monkeypatch):
    # Deterministic (review s13 M1): file mtime/size moved under GC-checkpointed WAL connections
    # of other tests. We prove "no database" by refusing any SQLite connection during the call,
    # and "nothing written" by comparing the database content, not its bytes on disk.
    put(env, "c-1", "failed", "-1 hours")
    add_rule(env)

    def dump():
        with sqlite3.connect(env["database"]) as connection:
            return list(connection.iterdump())

    content_before = dump()
    opened = []
    real_connect = sqlite3.connect

    def spy_connect(*args, **kwargs):
        opened.append(args[0] if args else kwargs.get("database"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    response = match(client, {"text": "je veux la voicebox", "keywords": ["voicebox"]})
    monkeypatch.setattr(sqlite3, "connect", real_connect)

    assert response.status_code == 200
    assert opened == []
    assert env["graph"].calls == []
    assert dump() == content_before


def test_keywords_match_without_a_database_creates_none(monkeypatch, tmp_path, client):
    database = tmp_path / "absent" / "processed.sqlite3"
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(database))
    monkeypatch.setenv("CONSOLE_INTERNAL_TOKEN", TOKEN)

    assert match(client, {"text": "voicebox", "keywords": ["voicebox"]}).status_code == 200
    assert not database.exists()


# --- s15 review queue (console ADR 042) ------------------------------------------------

import asyncio  # noqa: E402

from app.comment_review_store import CommentReviewItem, CommentReviewStore  # noqa: E402
from app.graph_client import GraphClient  # noqa: E402


REVIEW_TEXT = "secret-review-comment-text"
REPLY_TEXT = "secret-review-reply-text"
REVIEW_USER = "review-author-handle"


class ReviewGraph:
    """Records public replies (target id and message). `error` is raised instead of replying."""

    def __init__(self, error=None, gate=None):
        self.calls = []
        self.error = error
        self.gate = gate

    async def reply_to_instagram_comment(self, comment_id, message):
        self.calls.append(("instagram", comment_id, message))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return {"id": "posted-1"}

    async def reply_to_facebook_comment(self, comment_id, message):
        self.calls.append(("facebook", comment_id, message))
        if self.error is not None:
            raise self.error
        return {"id": "posted-1"}


@pytest.fixture
def review(env, monkeypatch):
    graph = ReviewGraph()
    monkeypatch.setattr(internal, "_graph_client_factory", lambda settings, http_client: graph)
    return {"graph": graph, "store": CommentReviewStore(env["database"]), "database": env["database"]}


def use_graph(monkeypatch, graph):
    monkeypatch.setattr(internal, "_graph_client_factory", lambda settings, http_client: graph)


def use_transport(monkeypatch, handler):
    """Real GraphClient on an httpx.MockTransport (F1, F5)."""

    def factory(settings, http_client):
        return GraphClient(
            access_token=settings.meta_page_access_token,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(internal, "_graph_client_factory", factory)


def seed_review(review, comment_id="111", *, platform="instagram", status="pending", age=None):
    review["store"].upsert_pending(
        CommentReviewItem(
            platform=platform,
            comment_id=comment_id,
            media_id="media-1",
            username=REVIEW_USER,
            text=REVIEW_TEXT,
            media_permalink=None,
            media_caption=None,
            reason="business_intent",
            score=90,
        )
    )
    with sqlite3.connect(review["database"]) as connection:
        connection.execute(
            "UPDATE comment_review_items SET status = ? WHERE platform = ? AND comment_id = ?",
            (status, platform, comment_id),
        )
        if age is not None:
            connection.execute(
                "UPDATE comment_review_items SET updated_at = datetime('now', ?) WHERE platform = ? AND comment_id = ?",
                (age, platform, comment_id),
            )


def review_row(review, comment_id="111", platform="instagram"):
    with sqlite3.connect(review["database"]) as connection:
        connection.row_factory = sqlite3.Row
        found = connection.execute(
            "SELECT * FROM comment_review_items WHERE platform = ? AND comment_id = ?", (platform, comment_id)
        ).fetchone()
        return dict(found) if found else None


def sent_rows(review):
    with sqlite3.connect(review["database"]) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbound_messages'"
        ).fetchone()
        if not exists:
            return []
        return [dict(found) for found in connection.execute("SELECT * FROM outbound_messages ORDER BY id")]


def review_post(client, action, comment_id="111", platform="instagram", body=None, headers=AUTH):
    return client.post(f"/internal/review/{platform}/{comment_id}/{action}", headers=headers, json=body)


def reply_review(client, comment_id="111", text=REPLY_TEXT, platform="instagram"):
    return review_post(client, "reply", comment_id, platform, {"text": text})


def assert_no_review_data(response):
    for forbidden in (REVIEW_TEXT, REPLY_TEXT, REVIEW_USER, "access_token", "page-token", "graph.facebook.com"):
        assert forbidden not in response.text


# nominal (AC 2)


def test_review_reply_posts_once_and_marks_replied(env, client, review):
    seed_review(review)

    response = reply_review(client, text=f"  {REPLY_TEXT}  ")

    assert response.status_code == 200
    assert response.json() == {"result": "replied", "status": "replied", "posted_reply_id": "posted-1"}
    assert review["graph"].calls == [("instagram", "111", REPLY_TEXT)]
    found = review_row(review)
    assert found["status"] == "replied"
    assert found["posted_reply_id"] == "posted-1"
    [sent] = sent_rows(review)
    assert sent["source_type"] == "manual_review"
    assert sent["message_type"] == "public_reply"
    assert sent["recipient_id"] == "111"
    assert sent["message_text"] == REPLY_TEXT
    assert_no_review_data(response)


def test_review_reply_on_facebook_targets_the_short_id(env, client, review):
    seed_review(review, "123_456", platform="facebook")

    response = reply_review(client, "123_456", platform="facebook")

    assert response.json()["result"] == "replied"
    assert review["graph"].calls == [("facebook", "456", REPLY_TEXT)]
    assert sent_rows(review)[0]["recipient_id"] == "456"
    assert sent_rows(review)[0]["source_id"] == "123_456"


def test_review_reply_treats_an_in_review_item(env, client, review):
    seed_review(review, status="in_review")
    assert reply_review(client).json()["result"] == "replied"
    assert len(review["graph"].calls) == 1


# idempotence (AC 6)


def test_two_concurrent_review_replies_post_exactly_once(env, review, monkeypatch):
    seed_review(review)
    gate = asyncio.Event()
    graph = ReviewGraph(gate=gate)
    use_graph(monkeypatch, graph)

    async def scenario():
        transport = httpx.ASGITransport(app=internal.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://internal") as http:
            url = "/internal/review/instagram/111/reply"
            first = asyncio.create_task(http.post(url, headers=AUTH, json={"text": REPLY_TEXT}))
            for _ in range(500):
                if graph.calls:
                    break
                await asyncio.sleep(0.01)
            assert graph.calls, "the first request never reached Graph"
            second = await asyncio.wait_for(http.post(url, headers=AUTH, json={"text": REPLY_TEXT}), 5)
            gate.set()
            return await asyncio.wait_for(first, 5), second

    first, second = asyncio.run(scenario())

    assert first.json() == {"result": "replied", "status": "replied", "posted_reply_id": "posted-1"}
    assert second.json() == {"result": "not_pending", "status": "sending"}
    assert len(graph.calls) == 1
    assert len(sent_rows(review)) == 1


def test_replayed_review_reply_after_success_sends_nothing(env, client, review):
    seed_review(review)
    reply_review(client)
    assert reply_review(client).json() == {"result": "not_pending", "status": "replied"}
    assert len(review["graph"].calls) == 1
    assert len(sent_rows(review)) == 1


@pytest.mark.parametrize("status", ["manually_replied", "skipped", "sending"])
def test_review_reply_on_a_handled_item_sends_nothing(env, client, review, status):
    seed_review(review, status=status)
    assert reply_review(client).json() == {"result": "not_pending", "status": status}
    assert review["graph"].calls == []
    assert sent_rows(review) == []


# Meta error (AC 5)


def test_certain_meta_refusal_leaves_the_item_pending_with_the_exact_message(env, client, review, monkeypatch):
    seed_review(review)
    error = GraphAPIError(
        "Meta Graph API error 100/33: (#100) Unsupported post request",
        code=100,
        subcode=33,
        http_status=400,
        meta_message="(#100) Unsupported post request",
    )
    use_graph(monkeypatch, ReviewGraph(error=error))

    response = reply_review(client)

    assert response.json() == {
        "result": "graph_error",
        "status": "pending",
        "code": 100,
        "subcode": 33,
        "message": "(#100) Unsupported post request",
    }
    found = review_row(review)
    assert found["status"] == "pending"
    assert found["reason"] == "business_intent"
    assert sent_rows(review) == []
    assert_no_review_data(response)


def test_meta_refusal_without_subcode_returns_null(env, client, review, monkeypatch):
    seed_review(review)
    use_graph(monkeypatch, ReviewGraph(error=GraphAPIError("x", code=190, http_status=400, meta_message="m")))
    body = reply_review(client).json()
    assert body["subcode"] is None
    assert body["code"] == 190


def test_meta_message_is_truncated_to_300(env, client, review, monkeypatch):
    seed_review(review)
    use_graph(monkeypatch, ReviewGraph(error=GraphAPIError("x", code=100, http_status=400, meta_message="m" * 500)))
    assert reply_review(client).json()["message"] == "m" * 300


def test_meta_message_never_carries_the_page_token(env, client, review, monkeypatch):
    seed_review(review)
    use_graph(
        monkeypatch,
        ReviewGraph(error=GraphAPIError("x", code=100, http_status=400, meta_message="bad token page-token here")),
    )
    response = reply_review(client)
    assert response.json()["message"] == "bad token [redacted] here"
    assert "page-token" not in response.text


def test_real_graph_client_400_is_a_graph_error(env, client, review, monkeypatch):
    seed_review(review)
    use_transport(
        monkeypatch,
        lambda request: httpx.Response(400, json={"error": {"message": "(#100) x", "code": 100, "error_subcode": 33}}),
    )
    assert reply_review(client).json() == {
        "result": "graph_error",
        "status": "pending",
        "code": 100,
        "subcode": 33,
        "message": "(#100) x",
    }
    assert review_row(review)["status"] == "pending"


# uncertain outcome (F1, F6): the item stays `sending`, never 500


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="<html>oops</html>"),
        httpx.Response(500, json={"error": {"message": "transient", "code": 2}}),
        httpx.Response(400, json={"error": {"message": "unknown", "code": 1}}),
        httpx.Response(400, text="<html>bad</html>"),
    ],
)
def test_ambiguous_graph_errors_are_uncertain(env, client, review, monkeypatch, response):
    seed_review(review)
    use_transport(monkeypatch, lambda request: response)

    answer = reply_review(client)

    assert answer.status_code == 200
    assert answer.json() == {"result": "uncertain", "status": "sending"}
    assert review_row(review)["status"] == "sending"
    assert sent_rows(review) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": {"message": "x", "code": 100}}),
        httpx.Response(503, json={"error": {"message": "x", "code": 4}}),
    ],
)
def test_a_real_meta_code_on_a_5xx_is_uncertain_not_a_refusal(env, client, review, monkeypatch, response):
    """The `400 <= status < 500` bound of is_certain_refusal: a 5xx never proves nothing was published."""
    seed_review(review)
    use_transport(monkeypatch, lambda request: response)

    answer = reply_review(client)

    assert answer.status_code == 200
    assert answer.json() == {"result": "uncertain", "status": "sending"}
    assert review_row(review)["status"] == "sending"
    assert sent_rows(review) == []


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_errors_are_uncertain(env, client, review, monkeypatch, exception):
    seed_review(review)

    def handler(request):
        raise exception("boom", request=request)

    use_transport(monkeypatch, handler)

    assert reply_review(client).json() == {"result": "uncertain", "status": "sending"}
    assert review_row(review)["status"] == "sending"
    assert sent_rows(review) == []


def test_failure_to_record_after_success_is_uncertain(env, client, review, monkeypatch):
    seed_review(review)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(CommentReviewStore, "complete_reply", boom)

    response = TestClient(internal.app, raise_server_exceptions=False).post(
        "/internal/review/instagram/111/reply", headers=AUTH, json={"text": REPLY_TEXT}
    )
    assert response.status_code == 200
    assert response.json() == {"result": "uncertain", "status": "sending"}


def test_failure_to_release_after_a_refusal_is_uncertain(env, client, review, monkeypatch):
    seed_review(review)
    use_graph(monkeypatch, ReviewGraph(error=GraphAPIError("x", code=100, http_status=400, meta_message="m")))

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(CommentReviewStore, "release_after_error", boom)

    response = TestClient(internal.app, raise_server_exceptions=False).post(
        "/internal/review/instagram/111/reply", headers=AUTH, json={"text": REPLY_TEXT}
    )
    assert response.status_code == 200
    assert response.json() == {"result": "uncertain", "status": "sending"}


def test_outbound_store_failure_answers_without_claiming_or_500(env, review, monkeypatch):
    """The outbound store is opened before the claim: if it fails, nothing is claimed nor sent."""
    seed_review(review)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(internal, "OutboundMessageStore", boom)

    response = TestClient(internal.app, raise_server_exceptions=False).post(
        "/internal/review/instagram/111/reply", headers=AUTH, json={"text": REPLY_TEXT}
    )
    assert response.status_code == 503
    assert response.json() == {"error": "internal_error"}
    assert review["graph"].calls == []
    assert review_row(review)["status"] == "pending"
    assert sent_rows(review) == []
    assert_no_review_data(response)


def test_uncertain_log_carries_the_exception_type_only(env, client, review, monkeypatch, caplog):
    seed_review(review)

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    use_transport(monkeypatch, handler)
    with caplog.at_level(logging.DEBUG):
        reply_review(client)
    assert "review_reply_uncertain ConnectError" in caplog.text


# skip (AC 3)


def _no_graph(settings, http_client):
    raise AssertionError("skip and release must never build a Graph client")


def test_skip_marks_skipped_without_graph(env, client, review, monkeypatch):
    seed_review(review)
    monkeypatch.setattr(internal, "_graph_client_factory", _no_graph)

    response = review_post(client, "skip")

    assert response.json() == {"result": "skipped", "status": "skipped"}
    assert review_row(review)["status"] == "skipped"
    assert review_row(review)["reviewed_at"] is not None


def test_skip_on_a_replied_item_is_not_pending(env, client, review, monkeypatch):
    seed_review(review, status="replied")
    monkeypatch.setattr(internal, "_graph_client_factory", _no_graph)
    assert review_post(client, "skip").json() == {"result": "not_pending", "status": "replied"}


# release (Q5)


def test_release_of_a_recent_sending_is_refused(env, client, review, monkeypatch):
    seed_review(review, status="sending", age="-1 minutes")
    monkeypatch.setattr(internal, "_graph_client_factory", _no_graph)
    assert review_post(client, "release").json() == {"result": "not_releasable", "status": "sending"}


def test_release_of_a_stale_sending_puts_it_back_pending(env, client, review, monkeypatch):
    seed_review(review, status="sending", age="-3 minutes")
    monkeypatch.setattr(internal, "_graph_client_factory", _no_graph)
    assert review_post(client, "release").json() == {"result": "released", "status": "pending"}
    assert review_row(review)["status"] == "pending"


def test_release_of_a_pending_item_is_refused(env, client, review, monkeypatch):
    seed_review(review)
    monkeypatch.setattr(internal, "_graph_client_factory", _no_graph)
    assert review_post(client, "release").json() == {"result": "not_releasable", "status": "pending"}


# validation


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "   "}, {"text": "a" * 2001}, {"text": "x", "extra": 1}, {}])
def test_invalid_reply_bodies_are_422(env, client, review, body):
    seed_review(review)
    response = review_post(client, "reply", body=body)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    assert review["graph"].calls == []
    assert review_row(review)["status"] == "pending"


def test_two_thousand_emojis_are_accepted(env, client, review):
    seed_review(review)
    assert reply_review(client, text="😀" * 2000).json()["result"] == "replied"


@pytest.mark.parametrize("action", ["reply", "skip", "release"])
@pytest.mark.parametrize(("platform", "comment_id"), [("facebook_dm", "111"), ("instagram", "abc"), ("tiktok", "111")])
def test_invalid_review_path_is_422(env, client, review, action, platform, comment_id):
    body = {"text": "x"} if action == "reply" else None
    assert review_post(client, action, comment_id, platform, body).status_code == 422
    assert review["graph"].calls == []


@pytest.mark.parametrize("action", ["reply", "skip", "release"])
def test_unknown_review_item_is_404(env, client, review, action):
    body = {"text": "x"} if action == "reply" else None
    response = review_post(client, action, "999", body=body)
    assert response.status_code == 404
    assert response.json() == {"error": "unknown_item"}
    assert review["graph"].calls == []


# leaks


def test_review_routes_log_neither_texts_nor_token(env, client, review, monkeypatch, caplog):
    seed_review(review, "111")
    seed_review(review, "222")
    seed_review(review, "333")
    with caplog.at_level(logging.DEBUG):
        responses = [reply_review(client, "111"), review_post(client, "skip", "222")]
        use_graph(monkeypatch, ReviewGraph(error=httpx.ConnectError("boom")))
        responses.append(reply_review(client, "333"))
    for response in responses:
        assert_no_review_data(response)
    for forbidden in (REVIEW_TEXT, REPLY_TEXT, TOKEN, "page-token"):
        assert forbidden not in caplog.text
