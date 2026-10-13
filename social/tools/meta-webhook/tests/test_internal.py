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
    # s16 (console ADR 044): conversation read routes, same auth and same absence from the public app.
    ("POST", "/internal/conversations", {"limit": 25}),
    ("POST", "/internal/conversations/messages", {"platform": "messenger", "ref": "dF8x", "cursor": None}),
    # s17 (console ADR 045): DM reply route, same auth and same absence from the public app.
    ("POST", "/internal/conversations/reply", {"platform": "messenger", "ref": "dF8x", "text": "x", "key": "k" * 16}),
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


# --- s16 conversation reads (console ADR 044) ------------------------------------------

from pathlib import Path  # noqa: E402

from app.inbox_read import encode_ref  # noqa: E402


PEER_NAME = "secret-peer-name"
PEER_USERNAME = "secret-peer-handle"
DM_TEXT = "secret-dm-text"
IG_USER_ID = "17841400000000000"
GRAPH_DATE = "2026-10-12T18:02:11+0000"


def graph_conversation(conversation_id, *, peer_id="fb-peer-1", text=DM_TEXT):
    return {
        "id": conversation_id,
        "updated_time": GRAPH_DATE,
        "participants": {
            "data": [
                {"id": "999", "name": "Page"},
                {"id": peer_id, "name": PEER_NAME, "username": PEER_USERNAME, "email": "peer@example.com"},
            ]
        },
        "messages": {"data": [{"id": f"m-{conversation_id}", "message": text, "from": {"id": peer_id},
                               "created_time": GRAPH_DATE}]},
    }


def graph_messages(count, *, start=0):
    return [
        {
            "id": f"m_{start + index}",
            "message": f"{DM_TEXT} {start + index}",
            "from": {"id": "999" if index % 2 else "fb-peer-1"},
            "created_time": GRAPH_DATE,
        }
        for index in range(count)
    ]


class InboxGraph:
    """Records calls. Per-platform values: a Graph payload, an exception to raise, or an asyncio.Event to wait on."""

    def __init__(self, lists=None, participants=None, messages=None):
        self.calls = []
        self.lists = lists or {}
        self.participants = participants
        self.messages = messages

    @staticmethod
    async def _answer(value, default):
        if isinstance(value, asyncio.Event):
            await value.wait()
        if isinstance(value, BaseException):
            raise value
        return default if value is None else value

    async def list_conversations(self, page_id, platform, *, limit, after=None):
        self.calls.append(("list_conversations", page_id, platform, limit, after))
        return await self._answer(self.lists.get(platform), {"data": []})

    async def get_conversation_participants(self, conversation_id):
        self.calls.append(("get_conversation_participants", conversation_id))
        default = {"id": conversation_id, "participants": graph_conversation(conversation_id)["participants"]}
        return await self._answer(self.participants, default)

    async def list_conversation_messages(self, conversation_id, *, limit, after=None):
        self.calls.append(("list_conversation_messages", conversation_id, limit, after))
        return await self._answer(self.messages, {"data": graph_messages(2)})


@pytest.fixture
def inbox(env, monkeypatch):
    monkeypatch.setenv("META_IG_USER_ID", IG_USER_ID)
    graph = InboxGraph()
    use_graph(monkeypatch, graph)
    return graph


def list_conversations(client, limit=25, headers=AUTH):
    return client.post("/internal/conversations", headers=headers, json={"limit": limit})


def read_thread(client, ref, *, platform="messenger", cursor=None, headers=AUTH, extra=None):
    body = {"platform": platform, "ref": ref, "cursor": cursor, **(extra or {})}
    return client.post("/internal/conversations/messages", headers=headers, json=body)


def assert_no_inbox_leak(response):
    for forbidden in ("access_token", "graph.facebook.com", "email", "peer@example.com", "page-token", TOKEN):
        assert forbidden not in response.text


# list: nominal


def test_conversations_reads_both_platforms(env, client, inbox):
    inbox.lists = {
        "instagram": {"data": [graph_conversation("ig_1", peer_id="ig-peer-1")]},
        "messenger": {"data": [graph_conversation("t_1")], "paging": {"next": "https://x/next"}},
    }

    response = list_conversations(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"instagram", "messenger"}
    assert body["instagram"]["result"] == "ok"
    assert body["instagram"]["has_more"] is False
    assert body["messenger"]["has_more"] is True
    assert body["messenger"]["conversations"] == [
        {
            "ref": encode_ref("t_1"),
            "updated_time": GRAPH_DATE,
            "peer": {"id": "fb-peer-1", "name": PEER_NAME, "username": PEER_USERNAME},
            "last_message": {"text": DM_TEXT, "from_me": False, "created_time": GRAPH_DATE},
        }
    ]
    assert sorted(inbox.calls) == [
        ("list_conversations", "999", "instagram", 25, None),
        ("list_conversations", "999", "messenger", 25, None),
    ]
    assert_no_inbox_leak(response)


def test_conversations_passes_the_limit_of_fifty(env, client, inbox):
    list_conversations(client, 50)
    assert {call[3] for call in inbox.calls} == {50}


@pytest.mark.parametrize("body", [{"limit": 30}, {"limit": 25, "extra": 1}, {}, {"limit": "x"}])
def test_conversations_rejects_invalid_bodies(env, client, inbox, body):
    response = client.post("/internal/conversations", headers=AUTH, json=body)
    assert response.status_code == 422
    assert inbox.calls == []


# list: one platform in error (AC 4)


def test_instagram_permission_error_leaves_messenger_intact(env, client, inbox):
    inbox.lists = {
        "instagram": GraphAPIError(
            "x", code=10, subcode=None, meta_message="(#10) x", meta_type="OAuthException", http_status=400
        ),
        "messenger": {"data": [graph_conversation("t_1")]},
    }

    body = list_conversations(client).json()

    assert body["instagram"] == {
        "result": "graph_error",
        "code": 10,
        "subcode": None,
        "type": "OAuthException",
        "message": "(#10) x",
    }
    assert body["messenger"]["result"] == "ok"
    assert len(body["messenger"]["conversations"]) == 1


def test_network_error_is_graph_unreachable(env, client, inbox):
    inbox.lists = {"instagram": httpx.ConnectError("boom"), "messenger": {"data": []}}
    body = list_conversations(client).json()
    assert body["instagram"] == {"result": "graph_unreachable"}
    assert body["messenger"] == {"result": "ok", "conversations": [], "has_more": False}


def test_a_slow_platform_is_unreachable_without_taking_the_other(env, client, inbox, monkeypatch):
    monkeypatch.setattr(internal, "PLATFORM_TIMEOUT_S", 0.05)
    inbox.lists = {"instagram": asyncio.Event(), "messenger": {"data": [graph_conversation("t_1")]}}

    response = list_conversations(client)

    assert response.status_code == 200
    assert response.json()["instagram"] == {"result": "graph_unreachable"}
    assert response.json()["messenger"]["result"] == "ok"
    assert len(response.json()["messenger"]["conversations"]) == 1


def test_graph_error_fields_are_coerced_and_bounded(env, client, inbox):
    inbox.lists = {"instagram": GraphAPIError("x", code="10", subcode=True, meta_type="x" * 100)}
    body = list_conversations(client).json()
    assert body["instagram"] == {"result": "graph_error", "code": None, "subcode": None, "type": "x" * 64,
                                 "message": ""}


def test_a_conversation_with_an_overlong_id_is_dropped(env, client, inbox):
    inbox.lists = {"messenger": {"data": [graph_conversation("x" * 300), graph_conversation("t_2"), "junk", {}]}}
    conversations = list_conversations(client).json()["messenger"]["conversations"]
    assert [item["ref"] for item in conversations] == [encode_ref("t_2")]


# list: the real GraphClient on a MockTransport


def test_real_client_400_with_type_is_the_exact_payload(env, client, inbox, monkeypatch):
    def handler(request):
        if request.url.params["platform"] == "instagram":
            return httpx.Response(400, json={"error": {"message": "(#3) x", "type": "OAuthException", "code": 3}})
        return httpx.Response(200, json={"data": [graph_conversation("t_1")]})

    use_transport(monkeypatch, handler)
    body = list_conversations(client).json()
    assert body["instagram"] == {"result": "graph_error", "code": 3, "subcode": None, "type": "OAuthException",
                                 "message": "(#3) x"}
    assert body["messenger"]["result"] == "ok"


def test_real_client_html_error_has_no_code_and_no_message(env, client, inbox, monkeypatch):
    use_transport(monkeypatch, lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    body = list_conversations(client).json()
    for platform in ("instagram", "messenger"):
        assert body[platform] == {"result": "graph_error", "code": None, "subcode": None, "type": None,
                                  "message": ""}


def test_real_client_message_never_carries_the_page_token(env, client, inbox, monkeypatch):
    use_transport(
        monkeypatch,
        lambda request: httpx.Response(400, json={"error": {"message": "bad page-token here", "code": 190}}),
    )
    response = list_conversations(client)
    assert response.json()["instagram"]["message"] == "bad [redacted] here"
    assert "page-token" not in response.text


def test_real_client_message_is_truncated_to_300(env, client, inbox, monkeypatch):
    use_transport(monkeypatch, lambda request: httpx.Response(400, json={"error": {"message": "m" * 500, "code": 1}}))
    assert list_conversations(client).json()["messenger"]["message"] == "m" * 300


def test_real_client_network_error_is_unreachable(env, client, inbox, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom")

    use_transport(monkeypatch, handler)
    body = list_conversations(client).json()
    assert body == {"instagram": {"result": "graph_unreachable"}, "messenger": {"result": "graph_unreachable"}}


# configuration


@pytest.mark.parametrize("variable", ["META_PAGE_ACCESS_TOKEN", "META_PAGE_ID"])
def test_unconfigured_page_is_503_without_graph(env, client, inbox, monkeypatch, variable):
    monkeypatch.setenv(variable, "")
    assert list_conversations(client).status_code == 503
    assert list_conversations(client).json() == {"error": "meta_unconfigured"}
    response = read_thread(client, encode_ref("t_1"))
    assert response.status_code == 503
    assert response.json() == {"error": "meta_unconfigured"}
    assert inbox.calls == []


def test_both_routes_work_without_any_database(env, client, inbox, monkeypatch, tmp_path):
    database = tmp_path / "absent" / "processed.sqlite3"
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(database))
    assert list_conversations(client).status_code == 200
    assert read_thread(client, encode_ref("t_1")).status_code == 200
    assert not database.parent.exists()


# thread


def test_messenger_thread_reads_participants_and_messages(env, client, inbox):
    inbox.messages = {"data": graph_messages(3), "paging": {"next": "https://x/next", "cursors": {"after": "AFTER-1"}}}

    response = read_thread(client, encode_ref("t_123"), cursor=encode_ref("BEFORE-1"))

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == "ok"
    assert body["peer"] == {"id": "fb-peer-1", "name": PEER_NAME, "username": PEER_USERNAME}
    assert [message["id"] for message in body["messages"]] == ["m_0", "m_1", "m_2"]
    assert [message["from_me"] for message in body["messages"]] == [False, True, False]
    assert body["older_cursor"] == encode_ref("AFTER-1")
    assert body["capped"] is False
    assert sorted(inbox.calls) == [
        ("get_conversation_participants", "t_123"),
        ("list_conversation_messages", "t_123", 20, "BEFORE-1"),
    ]
    assert_no_inbox_leak(response)


def test_messenger_thread_without_next_page_has_no_older_cursor(env, client, inbox):
    inbox.messages = {"data": graph_messages(3), "paging": {"cursors": {"after": "AFTER-1"}}}
    assert read_thread(client, encode_ref("t_123")).json()["older_cursor"] is None


def test_an_overlong_graph_cursor_is_dropped(env, client, inbox, caplog):
    inbox.messages = {"data": graph_messages(1), "paging": {"next": "n", "cursors": {"after": "c" * 2000}}}
    with caplog.at_level(logging.WARNING):
        body = read_thread(client, encode_ref("t_123")).json()
    assert body["older_cursor"] is None
    assert "inbox_cursor_too_long" in caplog.text


@pytest.mark.parametrize(("count", "capped"), [(20, True), (19, False)])
def test_instagram_thread_is_capped_at_twenty(env, client, inbox, count, capped):
    inbox.messages = {"data": graph_messages(count), "paging": {"next": "n", "cursors": {"after": "AFTER"}}}

    body = read_thread(client, encode_ref("ig_1"), platform="instagram").json()

    assert body["capped"] is capped
    assert body["older_cursor"] is None
    assert ("list_conversation_messages", "ig_1", 20, None) in inbox.calls


def test_instagram_thread_with_a_cursor_is_422(env, client, inbox):
    response = read_thread(client, encode_ref("ig_1"), platform="instagram", cursor=encode_ref("x"))
    assert response.status_code == 422
    assert inbox.calls == []


@pytest.mark.parametrize(("ref", "error"), [("!!", "invalid_request"), ("_w", "invalid_cursor")])
def test_invalid_refs_are_422(env, client, inbox, ref, error):
    response = read_thread(client, ref)
    assert response.status_code == 422
    assert response.json() == {"error": error}
    response = read_thread(client, encode_ref("t_1"), cursor=ref)
    assert response.status_code == 422
    assert response.json() == {"error": error}
    assert inbox.calls == []


@pytest.mark.parametrize("forged", ["me/accounts", "x?y", "../x"])
def test_forged_refs_never_reach_graph(env, client, inbox, forged):
    response = read_thread(client, encode_ref(forged))
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_cursor"}
    assert inbox.calls == []


@pytest.mark.parametrize(
    "extra", [{"platform": "facebook"}, {"unexpected": 1}]
)
def test_invalid_thread_bodies_are_422(env, client, inbox, extra):
    assert read_thread(client, encode_ref("t_1"), extra=extra).status_code == 422
    assert inbox.calls == []


def test_unknown_conversation_is_a_graph_error_100(env, client, inbox):
    inbox.messages = GraphAPIError("x", code=100, meta_message="(#100) x", meta_type="OAuthException", http_status=400)
    body = read_thread(client, encode_ref("t_0")).json()
    assert body == {"result": "graph_error", "code": 100, "subcode": None, "type": "OAuthException",
                    "message": "(#100) x"}


def test_a_participants_error_is_a_graph_error(env, client, inbox):
    inbox.participants = GraphAPIError("x", code=10, meta_message="(#10) x", http_status=400)
    assert read_thread(client, encode_ref("t_0")).json()["code"] == 10


def test_thread_network_error_is_unreachable(env, client, inbox):
    inbox.messages = httpx.ReadTimeout("slow")
    assert read_thread(client, encode_ref("t_0")).json() == {"result": "graph_unreachable"}


def test_slow_thread_is_unreachable(env, client, inbox, monkeypatch):
    monkeypatch.setattr(internal, "PLATFORM_TIMEOUT_S", 0.05)
    inbox.messages = asyncio.Event()
    response = read_thread(client, encode_ref("t_0"))
    assert response.status_code == 200
    assert response.json() == {"result": "graph_unreachable"}


def test_real_client_thread_paths(env, client, inbox, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"data": graph_messages(1)})
        return httpx.Response(200, json={"id": "t_9", "participants": {"data": [{"id": "fb-peer-1"}]}})

    use_transport(monkeypatch, handler)
    body = read_thread(client, encode_ref("t_9"), cursor=encode_ref("CUR")).json()
    assert body["result"] == "ok"
    paths = sorted(request.url.path for request in requests)
    assert paths == ["/v21.0/t_9", "/v21.0/t_9/messages"]
    messages_request = next(request for request in requests if request.url.path.endswith("/messages"))
    assert messages_request.url.params["after"] == "CUR"


# leaks


def test_inbox_routes_log_neither_personal_data_nor_token(env, client, inbox, monkeypatch, caplog):
    inbox.lists = {
        "instagram": GraphAPIError("x", code=10, meta_message="(#10) x"),
        "messenger": {"data": [graph_conversation("t_1")]},
    }
    ref = encode_ref("t_1")
    with caplog.at_level(logging.DEBUG):
        responses = [list_conversations(client), read_thread(client, ref)]
        inbox.lists = {"instagram": httpx.ConnectError("boom"), "messenger": httpx.ConnectError("boom")}
        inbox.messages = httpx.ConnectError("boom")
        responses += [list_conversations(client), read_thread(client, ref)]
    for response in responses:
        assert_no_inbox_leak(response)
    app_logs = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("app"))
    assert "inbox_graph_unreachable ConnectError" in app_logs
    for forbidden in (PEER_NAME, PEER_USERNAME, DM_TEXT, ref, "t_1", TOKEN, "page-token"):
        assert forbidden not in app_logs


def test_serve_keeps_httpx_quiet():
    """httpx logs the Graph URL (page token included) at INFO: serve.py must stay at WARNING."""
    serve = (Path(__file__).resolve().parents[1] / "app" / "serve.py").read_text(encoding="utf-8")
    assert "logging.basicConfig(level=logging.WARNING" in serve


# --- s17 DM reply (console ADR 045) -----------------------------------------------------

from app.dm_reply_store import DirectReplyStore  # noqa: E402
from app.outbound_message_store import OutboundMessageStore  # noqa: E402

DM_REPLY_TEXT = "secret-reply-text"
DM_KEY = "s17KeyAbcdefghij0123"
REPLY_NOW = datetime(2026, 10, 13, 9, 6, 0, tzinfo=timezone.utc)
LEAD = "lead-psid"


def reply_date(delta: timedelta) -> str:
    return (REPLY_NOW - delta).strftime("%Y-%m-%dT%H:%M:%S+0000")


def reply_participants(peer_id=LEAD):
    return {"id": "t_1", "participants": {"data": [{"id": "999", "name": "Page"},
                                                    {"id": peer_id, "name": PEER_NAME, "username": PEER_USERNAME}]}}


def reply_messages(*entries):
    """`entries` = (from_me, age) pairs, most recent first as Graph returns them."""
    return {
        "data": [
            {"id": f"m_{index}", "message": f"{DM_TEXT} {index}", "from": {"id": "999" if from_me else LEAD},
             "created_time": reply_date(age)}
            for index, (from_me, age) in enumerate(entries)
        ]
    }


class ReplyGraph(InboxGraph):
    """InboxGraph + `send_page_message`. `send` = a result dict, an exception, or an Event to wait on first."""

    def __init__(self, *, participants=None, messages=None, send=None):
        super().__init__(
            participants=participants if participants is not None else reply_participants(),
            messages=messages if messages is not None else reply_messages((False, timedelta(hours=1))),
        )
        self.send = send
        self.gate = None

    def sends(self):
        return [call for call in self.calls if call[0] == "send_page_message"]

    async def send_page_message(self, page_id, recipient_id, text):
        self.calls.append(("send_page_message", page_id, recipient_id, text))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.send, BaseException):
            raise self.send
        return self.send if self.send is not None else {"recipient_id": recipient_id, "message_id": "m_sent_1"}


@pytest.fixture
def dm(env, monkeypatch):
    graph = ReplyGraph()
    use_graph(monkeypatch, graph)
    monkeypatch.setattr(internal, "_now", lambda: REPLY_NOW)
    return graph


def reply_body(**overrides):
    body = {"platform": "messenger", "ref": encode_ref("t_1"), "text": DM_REPLY_TEXT, "key": DM_KEY}
    body.update(overrides)
    return body


def send_reply(client, body=None, headers=AUTH):
    return client.post("/internal/conversations/reply", headers=headers, json=body if body is not None else reply_body())


def dm_rows(env):
    with sqlite3.connect(env["database"]) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'console_dm_replies'"
        ).fetchone()
        if not exists:
            return []
        return [dict(found) for found in connection.execute("SELECT * FROM console_dm_replies")]


def outbound_rows(env):
    with sqlite3.connect(env["database"]) as connection:
        connection.row_factory = sqlite3.Row
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'outbound_messages'"
        ).fetchone()
        if not exists:
            return []
        return [dict(found) for found in connection.execute("SELECT * FROM outbound_messages ORDER BY id")]


def assert_no_reply_leak(response):
    for forbidden in ("access_token", "graph.facebook.com", "page-token", TOKEN, DM_REPLY_TEXT, PEER_NAME, LEAD):
        assert forbidden not in response.text


# nominal (AC 1, AC 3)


def test_messenger_reply_is_sent_once_to_the_conversation_peer_and_logged(env, client, dm):
    response = send_reply(client, reply_body(text=f"  {DM_REPLY_TEXT}\n"))

    assert response.status_code == 200
    assert response.json() == {"result": "sent", "message_id": "m_sent_1", "logged": True}
    assert dm.sends() == [("send_page_message", "999", LEAD, DM_REPLY_TEXT)]
    assert ("list_conversation_messages", "t_1", 20, None) in dm.calls
    [sent] = outbound_rows(env)
    assert {key: sent[key] for key in ("platform", "source_type", "source_id", "recipient_id", "message_type",
                                       "message_text", "meta_response_id", "status")} == {
        "platform": "facebook",
        "source_type": "console",
        "source_id": DM_KEY,
        "recipient_id": LEAD,
        "message_type": "dm_reply",
        "message_text": DM_REPLY_TEXT,
        "meta_response_id": "m_sent_1",
        "status": "sent",
    }
    [key_row] = dm_rows(env)
    assert key_row["idempotency_key"] == DM_KEY
    assert key_row["platform"] == "messenger"
    assert key_row["status"] == "sent"
    assert key_row["meta_response_id"] == "m_sent_1"
    assert set(key_row) == {"idempotency_key", "platform", "status", "result", "meta_response_id", "created_at",
                            "updated_at"}
    assert_no_reply_leak(response)


def test_instagram_reply_is_logged_on_the_instagram_platform(env, client, dm):
    response = send_reply(client, reply_body(platform="instagram", ref=encode_ref("ig_1")))
    assert response.json()["result"] == "sent"
    assert outbound_rows(env)[0]["platform"] == "instagram"
    assert dm_rows(env)[0]["platform"] == "instagram"


def test_reply_never_reads_an_older_page_of_the_thread(env, client, dm):
    send_reply(client)
    assert [call for call in dm.calls if call[0] == "list_conversation_messages"] == [
        ("list_conversation_messages", "t_1", 20, None)
    ]


def test_recipient_is_the_non_page_participant_even_after_a_page_message(env, client, dm):
    dm.messages = reply_messages((True, timedelta(minutes=5)), (False, timedelta(hours=2)))
    assert send_reply(client).json()["result"] == "sent"
    assert dm.sends()[0][2] == LEAD


def test_sent_without_message_id_answers_null(env, client, dm):
    dm.send = {"recipient_id": LEAD}
    assert send_reply(client).json() == {"result": "sent", "message_id": None, "logged": True}


# AC 5: no recipient, sender or tag from the console


@pytest.mark.parametrize(
    "extra",
    [
        {"recipient": {"id": "someone"}},
        {"recipient_id": "someone"},
        {"sender": "999"},
        {"from": "999"},
        {"page_id": "999"},
        {"tag": "HUMAN_AGENT"},
        {"messaging_type": "MESSAGE_TAG"},
    ],
)
def test_a_body_naming_a_recipient_or_sender_is_422_without_graph_or_claim(env, client, dm, extra):
    response = send_reply(client, reply_body(**extra))
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    assert dm.calls == []
    assert dm_rows(env) == []


def test_no_recipient_in_the_conversation_sends_nothing(env, client, dm):
    dm.participants = {"participants": {"data": [{"id": "999", "name": "Page"}]}}
    response = send_reply(client)
    assert response.json() == {"result": "no_recipient"}
    assert dm.sends() == []
    assert dm_rows(env)[0]["status"] == "failed"


# 24-hour window (AC 2), applied by Python just before the POST


def test_window_closed_at_exactly_twenty_four_hours(env, client, dm):
    dm.messages = reply_messages((False, timedelta(hours=24)))
    response = send_reply(client)
    assert response.json() == {"result": "window_closed", "closes_at": "2026-10-13T09:06:00.000Z"}
    assert dm.sends() == []
    assert dm_rows(env)[0]["status"] == "failed"
    assert outbound_rows(env) == []


def test_window_open_one_second_before_twenty_four_hours(env, client, dm):
    dm.messages = reply_messages((False, timedelta(hours=23, minutes=59, seconds=59)))
    assert send_reply(client).json()["result"] == "sent"
    assert len(dm.sends()) == 1


def test_only_page_messages_is_an_unknown_window_treated_as_closed(env, client, dm):
    dm.messages = reply_messages((True, timedelta(minutes=1)), (True, timedelta(hours=1)))
    assert send_reply(client).json() == {"result": "window_closed", "closes_at": None}
    assert dm.sends() == []


def test_empty_thread_is_an_unknown_window(env, client, dm):
    dm.messages = {"data": []}
    assert send_reply(client).json() == {"result": "window_closed", "closes_at": None}
    assert dm.sends() == []


# idempotency (ADR 045 §2)


def test_the_same_key_after_a_success_is_a_duplicate_without_graph(env, client, dm):
    assert send_reply(client).json()["result"] == "sent"
    calls = len(dm.calls)
    response = send_reply(client)
    assert response.json() == {"result": "duplicate", "status": "sent"}
    assert len(dm.calls) == calls
    assert len(dm.sends()) == 1
    assert len(outbound_rows(env)) == 1


def test_two_concurrent_replies_with_the_same_key_post_exactly_once(env, dm):
    dm.gate = asyncio.Event()

    async def scenario():
        transport = httpx.ASGITransport(app=internal.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://internal") as http:
            url = "/internal/conversations/reply"
            first = asyncio.create_task(http.post(url, headers=AUTH, json=reply_body()))
            for _ in range(500):
                if dm.sends():
                    break
                await asyncio.sleep(0.01)
            assert dm.sends(), "the first request never reached Graph"
            second = await asyncio.wait_for(http.post(url, headers=AUTH, json=reply_body()), 5)
            dm.gate.set()
            return await asyncio.wait_for(first, 5), second

    first, second = asyncio.run(scenario())

    assert first.json()["result"] == "sent"
    assert second.json() == {"result": "duplicate", "status": "sending"}
    assert len(dm.sends()) == 1
    assert len(outbound_rows(env)) == 1


@pytest.mark.parametrize(
    ("send", "first_result", "status"),
    [
        (GraphAPIError("x", code=10, subcode=2018278, meta_message="(#10) x", http_status=400), "graph_error", "failed"),
        (httpx.ConnectError("boom"), "uncertain", "uncertain"),
    ],
)
def test_a_replayed_finished_key_is_a_duplicate_with_its_status(env, client, dm, send, first_result, status):
    dm.send = send
    assert send_reply(client).json()["result"] == first_result
    calls = len(dm.calls)
    assert send_reply(client).json() == {"result": "duplicate", "status": status}
    assert len(dm.calls) == calls


def test_a_replayed_window_closed_key_is_a_failed_duplicate(env, client, dm):
    dm.messages = reply_messages((False, timedelta(days=2)))
    send_reply(client)
    assert send_reply(client).json() == {"result": "duplicate", "status": "failed"}
    assert dm.sends() == []


def test_a_failed_status_reread_answers_a_conservative_sending(env, client, dm, monkeypatch):
    send_reply(client)

    def boom(self, key):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(DirectReplyStore, "status_of", boom)
    assert send_reply(client).json() == {"result": "duplicate", "status": "sending"}


# everything before the POST: nothing left, never a 500 (R6)


def test_an_exception_while_reading_the_thread_is_graph_unreachable_and_failed(env, client, dm, monkeypatch, caplog):
    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(internal, "read_thread", boom)
    with caplog.at_level(logging.WARNING):
        response = send_reply(client)
    assert response.status_code == 200
    assert response.json() == {"result": "graph_unreachable"}
    assert dm.sends() == []
    assert dm_rows(env)[0]["status"] == "failed"
    assert "dm_reply_pre_send_failed RuntimeError" in caplog.text


def test_a_failing_finish_after_a_successful_send_still_answers_sent(env, client, dm, monkeypatch):
    real_finish = DirectReplyStore.finish

    def finish(self, key, status, result, meta_response_id=None):
        if status == "sent":
            raise sqlite3.OperationalError("locked")
        return real_finish(self, key, status, result, meta_response_id)

    monkeypatch.setattr(DirectReplyStore, "finish", finish)
    response = send_reply(client)
    assert response.json() == {"result": "sent", "message_id": "m_sent_1", "logged": True}
    assert dm_rows(env)[0]["status"] == "sending"
    assert send_reply(client).json() == {"result": "duplicate", "status": "sending"}
    assert len(dm.sends()) == 1


def test_a_failing_record_after_success_is_sent_but_unlogged(env, client, dm, monkeypatch, caplog):
    def boom(self, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(OutboundMessageStore, "record_sent", boom)
    with caplog.at_level(logging.WARNING):
        response = send_reply(client)
    assert response.json() == {"result": "sent", "message_id": "m_sent_1", "logged": False}
    assert dm_rows(env)[0]["status"] == "sent"
    assert "dm_reply_unlogged OperationalError" in caplog.text


def test_a_failing_finish_after_a_refusal_still_answers(env, client, dm, monkeypatch):
    dm.send = GraphAPIError("x", code=551, meta_message="(#551) x", http_status=400)

    def boom(self, *args, **kwargs):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(DirectReplyStore, "finish", boom)
    response = send_reply(client)
    assert response.status_code == 200
    assert response.json()["result"] == "graph_error"


def test_store_unavailable_before_the_claim_is_503_without_graph(env, dm, monkeypatch):
    def boom(self):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(DirectReplyStore, "_ensure_schema", boom)
    response = TestClient(internal.app, raise_server_exceptions=False).post(
        "/internal/conversations/reply", headers=AUTH, json=reply_body()
    )
    assert response.status_code == 503
    assert response.json() == {"error": "internal_error"}
    assert dm.calls == []


def test_a_claim_that_raises_is_a_certain_503_without_graph(env, dm, monkeypatch, caplog):
    # Console review n°1 (m1): the claim rolls back, nothing is taken nor sent: a certain
    # failure (console `dm_unavailable`, key kept), never a 500 read as uncertain.
    def boom(self, key, platform):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(DirectReplyStore, "claim", boom)
    response = TestClient(internal.app, raise_server_exceptions=False).post(
        "/internal/conversations/reply", headers=AUTH, json=reply_body()
    )
    assert response.status_code == 503
    assert response.json() == {"error": "internal_error"}
    assert dm.calls == []
    assert dm_rows(env) == []
    assert "dm_reply_store_unavailable OperationalError" in caplog.text


# Meta errors with the real GraphClient on a MockTransport (AC 4)


def reply_transport(monkeypatch, post, *, messages=None, get_error=None):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            if isinstance(post, BaseException):
                raise post
            return post
        if get_error is not None:
            if isinstance(get_error, BaseException):
                raise get_error
            return get_error
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=messages or reply_messages((False, timedelta(hours=1))))
        return httpx.Response(200, json=reply_participants())

    use_transport(monkeypatch, handler)
    monkeypatch.setattr(internal, "_now", lambda: REPLY_NOW)
    return posts


def meta_error(status, code, subcode=None, message="(#x) meta says no", error_type="OAuthException"):
    error = {"message": message, "type": error_type, "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    return httpx.Response(status, json={"error": error})


def test_real_client_posts_the_exact_send_api_request(env, client, monkeypatch):
    posts = reply_transport(monkeypatch, httpx.Response(200, json={"recipient_id": LEAD, "message_id": "m_9"}))
    assert send_reply(client).json() == {"result": "sent", "message_id": "m_9", "logged": True}
    [post] = posts
    assert post.url.path == "/v21.0/999/messages"
    assert json.loads(post.content) == {"recipient": {"id": LEAD}, "messaging_type": "RESPONSE",
                                        "message": {"text": DM_REPLY_TEXT}}


@pytest.mark.parametrize(
    ("response", "code", "subcode"),
    [(meta_error(400, 10, 2018278), 10, 2018278), (meta_error(400, 551), 551, None), (meta_error(400, 10), 10, None)],
)
def test_certain_meta_refusals_are_graph_errors_and_failed(env, client, monkeypatch, response, code, subcode):
    reply_transport(monkeypatch, response)
    body = send_reply(client).json()
    assert body == {"result": "graph_error", "code": code, "subcode": subcode, "type": "OAuthException",
                    "message": "(#x) meta says no"}
    assert dm_rows(env)[0]["status"] == "failed"
    assert outbound_rows(env) == []


@pytest.mark.parametrize(
    "post",
    [
        meta_error(500, 2),
        meta_error(400, 2),
        meta_error(400, 1),
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_ambiguous_send_outcomes_are_uncertain(env, client, monkeypatch, post):
    posts = reply_transport(monkeypatch, post)
    assert send_reply(client).json() == {"result": "uncertain"}
    assert len(posts) == 1
    assert dm_rows(env)[0]["status"] == "uncertain"
    assert outbound_rows(env) == []


def test_a_meta_code_on_a_5xx_is_uncertain(env, client, monkeypatch):
    reply_transport(monkeypatch, meta_error(500, 10, 2018278))
    assert send_reply(client).json() == {"result": "uncertain"}


def test_a_timeout_while_reading_the_thread_sends_nothing(env, client, monkeypatch):
    posts = reply_transport(monkeypatch, httpx.Response(200, json={}), get_error=httpx.ReadTimeout("slow"))
    assert send_reply(client).json() == {"result": "graph_unreachable"}
    assert posts == []
    assert dm_rows(env)[0]["status"] == "failed"


def test_an_unknown_conversation_is_a_graph_error_100_without_post(env, client, monkeypatch):
    posts = reply_transport(monkeypatch, httpx.Response(200, json={}), get_error=meta_error(400, 100))
    body = send_reply(client).json()
    assert body["result"] == "graph_error"
    assert body["code"] == 100
    assert posts == []
    assert dm_rows(env)[0]["status"] == "failed"


def test_a_meta_message_never_carries_the_page_token(env, client, monkeypatch):
    reply_transport(monkeypatch, meta_error(400, 190, message="bad page-token here"))
    response = send_reply(client)
    assert response.json()["message"] == "bad [redacted] here"
    assert "page-token" not in response.text


# invalid bodies (422) and configuration (503): no store, no Graph


@pytest.mark.parametrize(
    "overrides",
    [
        {"text": ""},
        {"text": "   \n "},
        {"text": "a" * 2001},
        {"platform": "instagram", "ref": encode_ref("ig_1"), "text": "é" * 500 + "a"},
        {"key": "k" * 15},
        {"key": "k" * 15 + "/"},
        {"key": "k" * 65},
        {"platform": "facebook"},
        {"ref": "!!"},
        {"text": 12},
    ],
)
def test_invalid_reply_bodies_are_422_without_store_or_graph(env, client, dm, overrides):
    response = send_reply(client, reply_body(**overrides))
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    assert dm.calls == []
    assert dm_rows(env) == []


def test_missing_fields_are_422(env, client, dm):
    for field in ("platform", "ref", "text", "key"):
        body = reply_body()
        del body[field]
        assert send_reply(client, body).status_code == 422
    assert dm.calls == []


def test_two_thousand_emojis_are_accepted_on_messenger(env, client, dm):
    assert send_reply(client, reply_body(text="😀" * 2000)).json()["result"] == "sent"


def test_a_thousand_bytes_are_accepted_on_instagram(env, client, dm):
    body = reply_body(platform="instagram", ref=encode_ref("ig_1"), text="é" * 500)
    assert send_reply(client, body).json()["result"] == "sent"


@pytest.mark.parametrize("forged", ["me/accounts", "x?y", "../x"])
def test_forged_reply_refs_never_reach_a_store_or_graph(env, client, dm, forged):
    response = send_reply(client, reply_body(ref=encode_ref(forged)))
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_cursor"}
    assert dm.calls == []
    assert dm_rows(env) == []


@pytest.mark.parametrize("variable", ["META_PAGE_ACCESS_TOKEN", "META_PAGE_ID"])
def test_unconfigured_page_is_503_without_claim(env, client, dm, monkeypatch, variable):
    monkeypatch.setenv(variable, "")
    response = send_reply(client)
    assert response.status_code == 503
    assert response.json() == {"error": "meta_unconfigured"}
    assert dm.calls == []
    assert dm_rows(env) == []


# s16 cuts (BLOCKERS s16): never more than the zod bounds of the console


def test_conversations_are_cut_to_the_limit(env, client, inbox):
    inbox.lists = {"messenger": {"data": [graph_conversation(f"t_{index}") for index in range(30)]}}
    body = list_conversations(client).json()
    assert len(body["messenger"]["conversations"]) == 25


def test_thread_messages_are_cut_to_twenty(env, client, inbox):
    inbox.messages = {"data": graph_messages(25)}
    body = read_thread(client, encode_ref("t_1")).json()
    assert [message["id"] for message in body["messages"]] == [f"m_{index}" for index in range(20)]


def test_instagram_cap_is_still_computed_on_the_raw_length(env, client, inbox):
    inbox.messages = {"data": graph_messages(25)}
    body = read_thread(client, encode_ref("ig_1"), platform="instagram").json()
    assert len(body["messages"]) == 20
    assert body["capped"] is True


# leaks


def test_reply_route_logs_neither_text_name_ref_key_nor_token(env, client, dm, monkeypatch, caplog):
    ref = encode_ref("t_1")
    with caplog.at_level(logging.DEBUG):
        responses = [send_reply(client)]
        dm.send = httpx.ConnectError("boom")
        responses.append(send_reply(client, reply_body(key="another-key-0123456789")))
        dm.messages = httpx.ConnectError("boom")
        responses.append(send_reply(client, reply_body(key="third-key-0123456789ab")))

        def boom(self, **kwargs):
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(OutboundMessageStore, "record_sent", boom)
        dm.send = None
        dm.messages = reply_messages((False, timedelta(hours=1)))
        responses.append(send_reply(client, reply_body(key="fourth-key-0123456789")))
    for response in responses:
        assert_no_reply_leak(response)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in (DM_REPLY_TEXT, PEER_NAME, PEER_USERNAME, LEAD, ref, "t_1", DM_KEY, "another-key", TOKEN,
                      "page-token"):
        assert forbidden not in logs
