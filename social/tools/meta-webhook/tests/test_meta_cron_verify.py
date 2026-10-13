"""s10-meta-backfill-job — app.meta_cron_verify, run by the hermes-console worker (job `meta-backfill`).

Every test runs the module on a `tmp_path` database and a fake Graph (`httpx.MockTransport`) that records
each call. No real `.env`, no real database, no network.
"""

import asyncio
import datetime as dt
import json
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

import app.meta_cron_verify as mcv
from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.config import Settings
from app.follow_gate_store import FollowGateStore
from app.outbound_message_store import OutboundMessageStore
from app.store import ProcessedCommentStore

TOKEN = "TEST_TOKEN_xyz"
PAGE_ID = "page-1"
IG_USER_ID = "ig-1"
SHELL_VARS = (
    "META_PAGE_ACCESS_TOKEN",
    "META_PAGE_ID",
    "META_IG_USER_ID",
    "META_OWNER_IDS",
    "META_OWNER_USERNAMES",
    "GRAPH_API_VERSION",
    "INTEREST_ONLY_KEYWORDS",
)


@pytest.fixture(autouse=True)
def _no_meta_shell_vars(monkeypatch):
    # pydantic-settings: process environment > env file. A META_* exported in the developer's
    # shell must never leak into these tests.
    for name in SHELL_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch):
    # The scan runs in process (P5): any subprocess call is a regression.
    def forbidden(*args, **kwargs):
        raise AssertionError(f"subprocess.run called: {args!r}")

    monkeypatch.setattr(subprocess, "run", forbidden)


# --------------------------------------------------------------------------- fixtures


class FakeGraph:
    """Routes Graph calls by method + path, records each one (method, path, query, body)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.ig_media: list[dict] = []
        self.ig_comments: dict[str, list[dict]] = {}
        self.fb_posts: list[dict] = []
        self.fb_comments: dict[str, list[dict]] = {}
        self.resolve: dict[str, dict] = {}
        self.messenger: list[dict] = []
        self.follow_conversations: dict[str, list[dict]] = {}
        self.overrides: list = []  # callables(call) -> httpx.Response | None (may raise)
        self._seq = 0

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        parts = path.strip("/").split("/", 1)
        path = "/" + (parts[1] if len(parts) > 1 else "")
        query = dict(request.url.params)
        body: dict = {}
        if request.content:
            ctype = request.headers.get("content-type", "")
            if "json" in ctype:
                body = json.loads(request.content)
            else:
                body = dict(parse_qsl(request.content.decode()))
        call = {"method": request.method, "path": path, "query": query, "body": body}
        self.calls.append(call)
        for override in self.overrides:
            result = override(call)
            if asyncio.iscoroutine(result):
                result = await result
            if result is not None:
                return result
        return self._default(call)

    def _default(self, call: dict) -> httpx.Response:
        method, path, query = call["method"], call["path"], call["query"]
        if method == "GET":
            if path == f"/{IG_USER_ID}/media":
                return httpx.Response(200, json={"data": self.ig_media})
            if path == f"/{PAGE_ID}/posts":
                return httpx.Response(200, json={"data": self.fb_posts})
            if path == f"/{PAGE_ID}/conversations":
                if "user_id" in query:
                    return httpx.Response(200, json={"data": self.follow_conversations.get(query["user_id"], [])})
                return httpx.Response(200, json={"data": self.messenger})
            if path.endswith("/comments"):
                owner = path[1:].rsplit("/", 1)[0]
                if owner in self.ig_comments:
                    return httpx.Response(200, json={"data": self.ig_comments[owner]})
                if owner in self.fb_comments:
                    return httpx.Response(200, json={"data": self.fb_comments[owner]})
                return httpx.Response(200, json={"data": []})
            cid = path[1:]
            if cid in self.resolve:
                return httpx.Response(200, json=self.resolve[cid])
            return httpx.Response(400, json={"error": {"message": "Unsupported get request", "code": 100}})
        if path == f"/{PAGE_ID}/messages":
            recipient = call["body"].get("recipient", {})
            return httpx.Response(200, json={"recipient_id": recipient.get("id") or "psid-1", "message_id": self._next("mid")})
        if path.endswith("/replies") or path.endswith("/comments"):
            return httpx.Response(200, json={"id": self._next("reply")})
        if path.endswith("/likes"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": {"message": "not found"}})


def graph_now(delta: dt.timedelta = dt.timedelta()) -> str:
    return (dt.datetime.now(dt.UTC) + delta).strftime("%Y-%m-%dT%H:%M:%S+0000")


def sqlite_now(delta: dt.timedelta = dt.timedelta()) -> str:
    return (dt.datetime.now(dt.UTC) + delta).strftime("%Y-%m-%d %H:%M:%S")


class Env:
    def __init__(self, tmp_path: Path, name: str = "runtime") -> None:
        self.dir = tmp_path / name
        self.dir.mkdir()
        self.db = self.dir / "processed_comments.sqlite3"
        self.env_file = self.dir / ".env"
        self.env_file.write_text(
            f"META_PAGE_ACCESS_TOKEN={TOKEN}\nMETA_PAGE_ID={PAGE_ID}\nMETA_IG_USER_ID={IG_USER_ID}\n"
            "META_OWNER_USERNAMES=dupflodev\n",
            encoding="utf-8",
        )
        self.processed = ProcessedCommentStore(self.db)
        self.review = CommentReviewStore(self.db)
        self.rules = CampaignRuleStore(self.db)
        self.outbound = OutboundMessageStore(self.db)
        self.gates = FollowGateStore(self.db)
        self.graph = FakeGraph()

    def argv(self) -> list[str]:
        return ["--env-file", str(self.env_file), "--db", str(self.db)]

    def run(self, capsys) -> tuple[int, list[str], str]:
        code = mcv.main(self.argv(), transport=self.graph.transport)
        out, err = capsys.readouterr()
        return code, [line for line in out.splitlines() if line.strip()], err

    def add_rule(self, source_task_id="task-1", *, platform="any", media_id=None, keywords=("proxy",),
                 public_reply_text="C'est envoyé !", dm_text="Voici le lien : https://example.com/r",
                 delivery_mode="direct") -> None:
        self.rules.upsert_rule(
            CampaignRule(
                source_task_id=source_task_id,
                name=source_task_id,
                platform=platform,
                media_id=media_id,
                keywords=list(keywords),
                public_reply_text=public_reply_text,
                dm_text=dm_text,
                delivery_mode=delivery_mode,
            )
        )

    def add_pending(self, platform: str, comment_id: str, text: str, *, media_id: str | None = "media-1",
                    username: str = "alice", reason: str = "direct_request", parent_id: str | None = None) -> None:
        self.review.upsert_pending(
            CommentReviewItem(
                platform=platform, comment_id=comment_id, media_id=media_id, username=username, text=text,
                media_permalink=None, media_caption=None, reason=reason, score=80, parent_id=parent_id,
            )
        )

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def outbound_count(self) -> int:
        return self.rows("SELECT COUNT(*) FROM outbound_messages")[0][0]


@pytest.fixture
def env(tmp_path) -> Env:
    return Env(tmp_path)


def parse_result(lines: list[str]) -> dict:
    assert lines[-1].startswith("CONSOLE_RESULT "), lines[-1]
    return json.loads(lines[-1][len("CONSOLE_RESULT "):])


def report_of(lines: list[str]) -> dict:
    report_lines = [line for line in lines if line.startswith("report ")]
    assert len(report_lines) == 1
    return json.loads(report_lines[0][len("report "):])


def counters_line(lines: list[str]) -> dict[str, int]:
    found = [line for line in lines if line.startswith("counters ")]
    assert len(found) == 1
    return {k: int(v) for k, v in (pair.split("=") for pair in found[0][len("counters "):].split())}


# --------------------------------------------------------------------------- runtime: paths and configuration


def _listing(*dirs: Path) -> set[str]:
    return {str(p) for d in dirs for p in d.rglob("*")}


@pytest.mark.parametrize("which", ["db_relative", "env_relative", "db_missing", "env_missing"])
def test_invalid_paths_exit_4_without_side_effect(tmp_path, monkeypatch, capsys, which):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = runtime / ".env"
    env_file.write_text(f"META_PAGE_ACCESS_TOKEN={TOKEN}\nMETA_PAGE_ID={PAGE_ID}\nMETA_IG_USER_ID={IG_USER_ID}\n")
    db = runtime / "processed_comments.sqlite3"
    sqlite3.connect(db).close()
    args = {"--env-file": str(env_file), "--db": str(db)}
    if which == "db_relative":
        args["--db"] = "data/processed_comments.sqlite3"
    elif which == "env_relative":
        args["--env-file"] = ".env"
    elif which == "db_missing":
        args["--db"] = str(runtime / "absent.sqlite3")
    else:
        args["--env-file"] = str(runtime / "absent.env")
    graph = FakeGraph()
    before = _listing(tmp_path)

    code = mcv.main([item for pair in args.items() for item in pair], transport=graph.transport)

    out, err = capsys.readouterr()
    assert code == 4
    assert "console: invalid_config" in err
    assert "CONSOLE_RESULT" not in out
    assert _listing(tmp_path) == before
    assert graph.calls == []
    assert TOKEN not in out + err


def test_empty_env_file_decoy_exits_4(tmp_path, capsys):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = runtime / ".env"
    env_file.write_text("")
    db = runtime / "processed_comments.sqlite3"
    sqlite3.connect(db).close()
    graph = FakeGraph()

    code = mcv.main(["--env-file", str(env_file), "--db", str(db)], transport=graph.transport)

    out, err = capsys.readouterr()
    assert code == 4
    assert "console: invalid_config" in err
    assert graph.calls == []


def test_settings_come_from_the_given_env_file_not_the_cwd(env, tmp_path, monkeypatch, capsys):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / ".env").write_text("META_PAGE_ACCESS_TOKEN=DECOY\nMETA_PAGE_ID=decoy-page\nMETA_IG_USER_ID=decoy-ig\n")
    monkeypatch.chdir(decoy)

    code, lines, _err = env.run(capsys)

    assert code == 0
    paths = {c["path"] for c in env.graph.calls}
    assert f"/{IG_USER_ID}/media" in paths
    assert f"/{PAGE_ID}/posts" in paths
    assert not any("decoy" in p for p in paths)
    assert all(c["query"].get("access_token") in (None, TOKEN) for c in env.graph.calls)
    assert lines[0] == f"console: db={env.db} env_file={env.env_file}"


# --------------------------------------------------------------------------- runtime: in-process scan


def test_scan_runs_in_process_with_limits_10_and_200(env, monkeypatch, capsys):
    seen = {}
    original = mcv.run_scan_from_fetcher

    async def spy(**kwargs):
        seen.update({k: kwargs[k] for k in ("platform", "media_limit", "comments_limit", "page_id", "ig_user_id")})
        return await original(**kwargs)

    monkeypatch.setattr(mcv, "run_scan_from_fetcher", spy)
    env.add_rule(media_id="media-1")
    env.graph.ig_media = [{"id": "media-1"}]
    env.graph.ig_comments = {
        "media-1": [
            {
                "id": "c-root",
                "text": "Super vidéo",
                "username": "bob",
                "replies": {"data": [{"id": "c-thread", "text": "proxy stp", "username": "carol"}]},
            }
        ]
    }
    env.graph.fb_posts = [{"id": "post-1"}]

    code, lines, _err = env.run(capsys)

    assert code == 0
    assert seen == {"platform": "all", "media_limit": 10, "comments_limit": 200, "page_id": PAGE_ID, "ig_user_id": IG_USER_ID}
    media_gets = [c for c in env.graph.calls if c["method"] == "GET" and c["path"] in (f"/{IG_USER_ID}/media", f"/{PAGE_ID}/posts")]
    assert {c["path"] for c in media_gets} == {f"/{IG_USER_ID}/media", f"/{PAGE_ID}/posts"}
    assert all(c["query"]["limit"] == "10" for c in media_gets)
    assert any(c["path"] == "/media-1/comments" for c in env.graph.calls)
    assert any(c["path"] == "/post-1/comments" for c in env.graph.calls)
    # The thread reply matching the active rule was queued by the repo's scanner, then served.
    row = env.rows("SELECT reason, status FROM comment_review_items WHERE comment_id='c-thread'")
    assert row == [("thread_reply", "replied")]
    report = report_of(lines)
    assert report["scan"]["ok"] is True
    assert report["scan"]["inserted_pending"] >= 1


def test_conversational_comment_is_queued_then_left_pending_without_any_post(env, capsys):
    env.add_rule(media_id="media-1")
    env.graph.ig_media = [{"id": "media-1"}]
    text = "Pourquoi utiliser cursor plutôt que codex"
    env.graph.ig_comments = {"media-1": [{"id": "c-conv", "text": text, "username": "dave"}]}
    env.graph.resolve = {"c-conv": {"id": "c-conv", "text": text, "media": {"id": "media-1"}}}

    code, _lines, _err = env.run(capsys)

    assert code == 0
    assert env.rows("SELECT reason, status FROM comment_review_items WHERE comment_id='c-conv'") == [("basic_reply", "pending")]
    assert env.graph.posts() == []


# --------------------------------------------------------------------------- runtime: output contract


def test_output_lines_in_order_with_console_result_last(env, capsys):
    env.add_rule(media_id="media-1")
    env.add_pending("instagram", "c-1", "proxy svp")

    code, lines, err = env.run(capsys)

    assert code == 0
    assert lines[0].startswith("console: db=")
    report_idx = next(i for i, line in enumerate(lines) if line.startswith("report "))
    counters_idx = next(i for i, line in enumerate(lines) if line.startswith("counters "))
    assert report_idx < counters_idx == len(lines) - 2
    json.loads(lines[report_idx][len("report "):])
    result = parse_result(lines)
    assert result["status"] == "ok"
    assert all(isinstance(v, int) for v in result["counters"].values())
    assert {"examined", "processed_new", "already_processed", "failed"} <= set(result["counters"])
    assert not any('"status": "empty"' in line or '"status":"empty"' in line for line in lines)
    assert TOKEN not in "\n".join(lines) + err


def test_run_without_any_action_keeps_the_same_structure(env, capsys):
    code, lines, _err = env.run(capsys)

    assert code == 0
    assert lines[0].startswith("console: db=")
    assert lines[-2].startswith("counters ")
    assert lines[-3].startswith("report ")
    assert parse_result(lines)["status"] == "ok"


# --------------------------------------------------------------------------- runtime: scan failure


def test_scan_failure_exits_3_but_still_serves_the_queue(env, capsys):
    env.add_rule(media_id="media-1")
    env.add_pending("instagram", "c-1", "proxy svp")

    def media_error(call):
        if call["method"] == "GET" and call["path"] == f"/{IG_USER_ID}/media":
            return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token", "code": 190}})
        return None

    env.graph.overrides.append(media_error)

    code, lines, err = env.run(capsys)

    assert code == 3
    report = report_of(lines)
    assert report["scan"]["ok"] is False
    assert report["scan"]["error"].startswith("GraphAPIError: ")
    assert any(c["path"] == f"/{PAGE_ID}/messages" for c in env.graph.posts())
    assert env.rows("SELECT status FROM comment_review_items WHERE comment_id='c-1'") == [("replied",)]
    assert lines[-1].startswith("counters ")
    assert "CONSOLE_RESULT" not in "\n".join(lines)
    assert TOKEN not in "\n".join(lines) + err


def test_scan_exceeding_its_bound_exits_3(env, monkeypatch, capsys):
    monkeypatch.setattr(mcv, "SCAN_TIMEOUT_SEC", 0.05)

    async def slow(call):
        if call["path"] == f"/{IG_USER_ID}/media":
            await asyncio.sleep(1)
        return None

    env.graph.overrides.append(slow)

    code, lines, _err = env.run(capsys)

    assert code == 3
    assert report_of(lines)["scan"]["error"].startswith("TimeoutError")
    assert "CONSOLE_RESULT" not in "\n".join(lines)


# --------------------------------------------------------------------------- runtime: secrets


LEAKY_URL = f"https://graph.facebook.com/v21.0/x?access_token={TOKEN}&fields=id"


def test_token_in_a_scan_exception_is_redacted(env, capsys):
    def boom(call):
        if call["path"] == f"/{IG_USER_ID}/media":
            raise httpx.ConnectError(f"connection failed for {LEAKY_URL}")
        return None

    env.graph.overrides.append(boom)

    code, lines, err = env.run(capsys)

    assert code == 3
    assert "access_token=<redacted>" in report_of(lines)["scan"]["error"]
    assert TOKEN not in "\n".join(lines) + err


def test_token_in_a_sweep_network_error_is_redacted(env, capsys):
    env.gates.create_pending(platform="facebook", comment_id="fb-gate", sender_id="psid-9", campaign="task-1",
                             keyword="proxy", dm_text="Voici le lien")
    old = sqlite_now(-dt.timedelta(hours=1))
    con = sqlite3.connect(env.db)
    con.execute("UPDATE follow_gates SET created_at=?, updated_at=?", (old, old))
    con.commit()
    con.close()
    env.graph.follow_conversations = {
        "psid-9": [{"messages": {"data": [{"message": "✅ J’ai suivi", "from": {"id": "psid-9"}, "created_time": graph_now()}]}}]
    }

    def boom(call):
        if call["method"] == "POST" and call["path"] == f"/{PAGE_ID}/messages":
            raise httpx.ConnectError(f"send failed {LEAKY_URL}")
        return None

    env.graph.overrides.append(boom)

    code, lines, err = env.run(capsys)

    assert code == 0
    sweep = report_of(lines)["follow_gate_sweep"]
    assert sweep[0]["stage"] == "unlock_network_error"
    assert "access_token=<redacted>" in sweep[0]["error"]
    assert TOKEN not in "\n".join(lines) + err


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"GET {LEAKY_URL}", "GET https://graph.facebook.com/v21.0/x?access_token=<redacted>&fields=id"),
        (f"url ends access_token={TOKEN}", "url ends access_token=<redacted>"),
        (f"quoted 'access_token={TOKEN}' and \"access_token={TOKEN}\"",
         "quoted 'access_token=<redacted>' and \"access_token=<redacted>\""),
        (f"bare token {TOKEN} here", "bare token <redacted> here"),
    ],
)
def test_scrub(tmp_path, text, expected):
    mcv.configure(tmp_path / "db.sqlite3", Settings(_env_file=None, meta_page_access_token=TOKEN))
    assert mcv._scrub(text) == expected


def test_scrub_with_an_empty_token_leaves_text_unchanged(tmp_path):
    mcv.configure(tmp_path / "db.sqlite3", Settings(_env_file=None, meta_page_access_token=""))
    assert mcv._scrub("nothing secret here") == "nothing secret here"


# --------------------------------------------------------------------------- counters (AC 2, Q1, Q1-bis, P3)

PROCESSED_NEW_STAGES = [
    "processed",
    "public_reply_only",
    "follow_gate_pending",
    "invitation_help_replied",
    "public_fallback_dm_request_private_expired",
    "messenger_keyword_dm_sent",
    "gate_unlocked_by_sweep",
    "public_reply_after_dm",
]
FAILED_STAGES = [
    "resolve_failed",
    "instagram_follow_gate_disabled",
    "follow_gate_missing_sender_id",
    "messenger_scan_failed",
    "unlock_rejected",
    "unlock_network_error",
    "invitation_help_failed",
    "public_only_failed",
    "follow_gate_prompt_failed",
    "public_failed_after_follow_gate_prompt",
    "dm_failed",
    "public_failed_after_dm",
    "messenger_dm_send_failed",
    "instagram_gate_blocked_webhook_signature",
]


@pytest.mark.parametrize("stage", PROCESSED_NEW_STAGES)
def test_bucket_processed_new(stage):
    assert mcv.bucket_for({"ok": True, "stage": stage}) == "processed_new"


@pytest.mark.parametrize("stage", FAILED_STAGES)
def test_bucket_failed(stage):
    assert mcv.bucket_for({"ok": False, "stage": stage}) == "failed"


def test_bucket_special_cases():
    assert mcv.bucket_for({"ok": True, "stage": "already_invitation_help_replied"}) == "already_processed"
    assert mcv.bucket_for({"ok": False, "stage": "unlock_skipped_no_confirmation_evidence"}) is None
    assert mcv.bucket_for({"ok": True, "stage": "some_future_stage"}) == "processed_new"
    assert mcv.bucket_for({"ok": False, "stage": "some_future_stage"}) == "failed"


def _seed_processed(env: Env, platform: str, comment_id: str, status: str, *, public: int = 0, dm: int = 0) -> None:
    con = sqlite3.connect(env.db)
    con.execute(
        "INSERT INTO processed_comments(platform,comment_id,keyword,status,public_reply_sent,dm_sent) VALUES(?,?,?,?,?,?)",
        (platform, comment_id, "proxy", status, public, dm),
    )
    con.commit()
    con.close()


def _seed_counter_queue(env: Env) -> None:
    env.add_rule()  # platform any, keyword "proxy", no media lock
    env.add_pending("instagram", "c-done", "proxy svp")
    _seed_processed(env, "instagram", "c-done", "processed", public=1, dm=1)
    env.add_pending("instagram", "c-blocked", "proxy svp")
    _seed_processed(env, "instagram", "c-blocked", "private_reply_blocked", public=1)
    env.add_pending("facebook", "c-norule", "hello there my friends", media_id="post-1")
    env.graph.resolve["c-norule"] = {"id": "c-norule", "message": "hello there my friends"}
    env.add_pending("instagram", "c-unresolvable", "proxy", media_id=None)  # resolve -> Graph 400
    env.graph.messenger = [
        {
            "messages": {
                "data": [
                    {"id": "mm-1", "message": "proxy", "from": {"id": "psid-1", "name": "Ann"}, "created_time": graph_now(-dt.timedelta(minutes=10))},
                    {"id": "mm-2", "message": "proxy please", "from": {"id": "psid-2", "name": "Ben"}, "created_time": graph_now(-dt.timedelta(minutes=5))},
                    {"id": "mm-3", "message": "proxy", "from": {"id": "psid-3", "name": "Cid"}, "created_time": graph_now(-dt.timedelta(hours=5))},
                ]
            }
        }
    ]
    _seed_processed(env, "facebook_dm", "mm-1", "processed", dm=1)


def test_counters_exact_values_for_a_scenario_exercising_each_one(env, capsys):
    _seed_counter_queue(env)
    env.graph.ig_media = [{"id": "media-1"}]
    noise = [{"id": "s1", "text": "Merci", "username": "x"}, {"id": "s2", "text": "top", "username": "y"}]
    env.graph.ig_comments = {"media-1": [noise[0], noise[0], noise[1]]}  # s1 returned twice by Graph
    env.graph.fb_posts = [{"id": "post-1"}]
    env.graph.fb_comments = {"post-1": [{"id": "s3", "message": "bravo", "from": {"id": "u3", "name": "Zed"}}]}

    code, lines, _err = env.run(capsys)

    assert code == 0
    counters = parse_result(lines)["counters"]
    assert counters == {
        "examined": 3,
        "processed_new": 1,
        "already_processed": 3,
        "failed": 1,
        "scanned": 3,
        "pending_read": 4,
        "messenger_considered": 2,
        "inserted_pending": 0,
        "unmatched": 1,
    }
    assert counters_line(lines) == counters
    assert len([c for c in env.graph.posts() if c["path"] == f"/{PAGE_ID}/messages"]) == counters["processed_new"]


def test_examined_counts_graph_comments_only_not_the_local_queue(env, capsys):
    _seed_counter_queue(env)

    code, lines, _err = env.run(capsys)

    assert code == 0
    counters = parse_result(lines)["counters"]
    assert counters["examined"] == 0
    assert counters["scanned"] == 0
    assert counters["pending_read"] == 4
    assert counters["messenger_considered"] == 2
    assert counters_line(lines) == counters


# --------------------------------------------------------------------------- idempotence (AC 5, Q4, P4)

DM_TEXT = "Voici le lien : https://example.com/r"
GATE_DM_TEXT = "Le lien du gate : https://example.com/g"


def _messages_posts(env: Env, since: int = 0) -> list[dict]:
    return [c for c in env.graph.calls[since:] if c["method"] == "POST" and c["path"] == f"/{PAGE_ID}/messages"]


def _actions(lines: list[str]) -> list[dict]:
    return report_of(lines)["pending_keyword_actions"]


def _stage_of(lines: list[str], comment_id: str) -> str:
    return next(a["stage"] for a in _actions(lines) if a.get("comment_id") == comment_id)


def _seed_every_sending_path(env: Env) -> None:
    env.add_rule("r-direct", keywords=("proxy",), dm_text=DM_TEXT)
    env.add_rule("r-public", keywords=("mcp",), dm_text=mcv.PUBLIC_REPLY_ONLY_DM_MARKER, public_reply_text="")
    env.add_rule("r-gate", platform="facebook", keywords=("gate",), dm_text=GATE_DM_TEXT, delivery_mode="follow_gate")
    # DM + public, Instagram and Facebook
    env.add_pending("instagram", "ig-dm", "proxy svp")
    env.add_pending("facebook", "fb-dm", "proxy svp", media_id="post-1")
    # public reply only
    env.add_pending("instagram", "ig-pub", "mcp", reason="possible_unconfigured_keyword")
    # Facebook follow gate
    env.add_pending("facebook", "fb-gate", "gate please", media_id="post-1")
    env.graph.resolve["fb-gate"] = {"id": "fb-gate", "message": "gate please", "from": {"id": "psid-g", "name": "Gil"}}
    # "nothing received" complaint with delivery evidence on the parent
    env.add_pending("facebook", "fb-complaint", "j'ai rien reçu", media_id="post-1", parent_id="fb-parent")
    _seed_processed(env, "facebook", "fb-parent", "processed", public=1, dm=1)
    # Facebook 10901 fallback
    env.add_pending("facebook", "fb-10901", "proxy", media_id="post-1")

    def expired(call):
        if call["method"] == "POST" and call["path"] == f"/{PAGE_ID}/messages" \
                and call["body"].get("recipient", {}).get("comment_id") == "fb-10901":
            return httpx.Response(400, json={"error": {"message": "(#10901) This message is sent outside of allowed window.", "code": 10901}})
        return None

    env.graph.overrides.append(expired)
    # Messenger keyword DM
    env.graph.messenger = [
        {"messages": {"data": [{"id": "mm-1", "message": "proxy", "from": {"id": "psid-1", "name": "Ann"}, "created_time": graph_now(-dt.timedelta(minutes=3))}]}}
    ]
    # follow gate unlocked by the sweep with "J'ai suivi" evidence
    env.gates.create_pending(platform="facebook", comment_id="fb-old-gate", sender_id="psid-s", campaign="r-gate",
                             keyword="gate", dm_text=GATE_DM_TEXT)
    old = sqlite_now(-dt.timedelta(hours=1))
    con = sqlite3.connect(env.db)
    con.execute("UPDATE follow_gates SET created_at=?, updated_at=? WHERE comment_id='fb-old-gate'", (old, old))
    con.commit()
    con.close()
    env.graph.follow_conversations = {
        "psid-s": [{"messages": {"data": [{"message": "✅ J’ai suivi", "from": {"id": "psid-s"}, "created_time": graph_now(-dt.timedelta(minutes=1))}]}}]
    }


def test_ac5_second_run_on_the_same_comments_sends_nothing_and_adds_no_outbound_row(env, capsys):
    _seed_every_sending_path(env)

    code, lines, _err = env.run(capsys)

    assert code == 0
    stages = {a.get("comment_id") or a.get("message_id"): a["stage"] for key in ("pending_keyword_actions", "messenger_dm_actions", "follow_gate_sweep") for a in report_of(lines)[key]}
    assert stages == {
        "ig-dm": "processed",
        "fb-dm": "processed",
        "ig-pub": "public_reply_only",
        "fb-gate": "follow_gate_pending",
        "fb-complaint": "invitation_help_replied",
        "fb-10901": "public_fallback_dm_request_private_expired",
        "mm-1": "messenger_keyword_dm_sent",
        "fb-old-gate": "gate_unlocked_by_sweep",
    }
    first = env.outbound_count()
    assert first > 0
    calls_before = len(env.graph.calls)

    code2, lines2, _err2 = env.run(capsys)

    assert code2 == 0
    assert env.outbound_count() == first
    assert [c for c in env.graph.calls[calls_before:] if c["method"] == "POST"] == []
    counters = parse_result(lines2)["counters"]
    assert counters["processed_new"] == 0
    assert counters["already_processed"] > 0


def test_dm_is_never_resent_after_a_failed_public_reply(env, capsys):
    env.add_rule(keywords=("proxy",), dm_text=DM_TEXT)
    env.add_pending("instagram", "ig-hole", "proxy svp")
    public_fails = {"on": True}

    def public_error(call):
        if public_fails["on"] and call["method"] == "POST" and call["path"] == "/ig-hole/replies":
            return httpx.Response(400, json={"error": {"message": "temporary failure", "code": 2}})
        return None

    env.graph.overrides.append(public_error)

    _code, lines, _err = env.run(capsys)

    assert _stage_of(lines, "ig-hole") == "public_failed_after_dm"
    assert env.rows("SELECT status, dm_sent, public_reply_sent FROM processed_comments WHERE comment_id='ig-hole'") == [("failed", 1, 0)]
    assert env.rows("SELECT message_type FROM outbound_messages WHERE source_id='ig-hole'") == [("private_reply",)]

    public_fails["on"] = False
    since = len(env.graph.calls)
    _code, lines, _err = env.run(capsys)

    assert _messages_posts(env, since) == []
    assert [c["path"] for c in env.graph.calls[since:] if c["method"] == "POST"] == ["/ig-hole/replies"]
    assert _stage_of(lines, "ig-hole") == "public_reply_after_dm"
    assert env.rows("SELECT status, dm_sent, public_reply_sent FROM processed_comments WHERE comment_id='ig-hole'") == [("processed", 1, 1)]

    since = len(env.graph.calls)
    rows_before = env.outbound_count()
    _code, _lines, _err = env.run(capsys)

    assert [c for c in env.graph.calls[since:] if c["method"] == "POST"] == []
    assert env.outbound_count() == rows_before
    assert len(_messages_posts(env)) == 1
    assert env.rows("SELECT message_type FROM outbound_messages WHERE source_id='ig-hole' ORDER BY id") == [("private_reply",), ("public_reply",)]


def test_follow_gate_prompt_is_never_resent_after_a_failed_public_reply(env, capsys):
    env.add_rule("r-gate", platform="facebook", keywords=("gate",), dm_text=GATE_DM_TEXT, delivery_mode="follow_gate")
    env.add_pending("facebook", "fbgatehole", "gate please", media_id="post-1")
    env.graph.resolve["fbgatehole"] = {"id": "fbgatehole", "message": "gate please", "from": {"id": "psid-h", "name": "Hal"}}
    public_fails = {"on": True}

    def public_error(call):
        if public_fails["on"] and call["method"] == "POST" and call["path"] == "/fbgatehole/comments":
            return httpx.Response(400, json={"error": {"message": "temporary failure", "code": 2}})
        return None

    env.graph.overrides.append(public_error)

    _code, lines, _err = env.run(capsys)
    assert _stage_of(lines, "fbgatehole") == "public_failed_after_follow_gate_prompt"
    assert env.rows("SELECT status, dm_sent, public_reply_sent FROM processed_comments WHERE comment_id='fbgatehole'") == [("follow_gate_pending", 0, 0)]

    public_fails["on"] = False
    _code, lines, _err = env.run(capsys)
    assert _stage_of(lines, "fbgatehole") == "public_reply_after_dm"

    _code, _lines, _err = env.run(capsys)

    assert len(_messages_posts(env)) == 1
    assert env.rows("SELECT COUNT(*) FROM outbound_messages WHERE source_id='fbgatehole' AND message_type='follow_gate_prompt'") == [(1,)]
    assert env.rows("SELECT COUNT(*) FROM follow_gates WHERE comment_id='fbgatehole'") == [(1,)]
    assert env.rows("SELECT status, dm_sent, public_reply_sent FROM processed_comments WHERE comment_id='fbgatehole'") == [("follow_gate_pending", 0, 1)]


def test_public_reply_is_persisted_before_the_like(env, capsys):
    env.add_rule("r-public", keywords=("mcp",), dm_text=mcv.PUBLIC_REPLY_ONLY_DM_MARKER, public_reply_text="")
    env.add_pending("instagram", "ig-like", "mcp")

    def like_breaks(call):
        if call["method"] == "POST" and call["path"] == "/ig-like/likes":
            raise httpx.ConnectError("like failed")
        return None

    env.graph.overrides.append(like_breaks)

    with pytest.raises(httpx.ConnectError):  # uncaught network error: the run fails (exit 1, existing behaviour)
        mcv.main(env.argv(), transport=env.graph.transport)
    capsys.readouterr()

    assert env.rows("SELECT status, public_reply_sent, like_sent FROM processed_comments WHERE comment_id='ig-like'") == [("public_reply_only", 1, 0)]
    assert env.rows("SELECT message_type FROM outbound_messages WHERE source_id='ig-like'") == [("public_reply",)]

    since = len(env.graph.calls)
    code, _lines, _err = env.run(capsys)

    assert code == 0
    assert [c for c in env.graph.calls[since:] if c["method"] == "POST"] == []
    assert env.rows("SELECT COUNT(*) FROM outbound_messages WHERE source_id='ig-like'") == [(1,)]


# ---- equivalence with the production persist_* functions (kept unchanged in the module)

TIMESTAMPS = {"created_at", "updated_at", "processed_at", "reviewed_at", "unlocked_at"}


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = {}
    try:
        for table, order in (("processed_comments", "platform, comment_id"), ("comment_review_items", "platform, comment_id"),
                             ("outbound_messages", "id"), ("follow_gates", "platform, comment_id")):
            rows = []
            for row in con.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                rows.append(tuple((k, (row[k] is not None) if k in TIMESTAMPS else row[k]) for k in row.keys() if k != "id"))
            out[table] = rows
    finally:
        con.close()
    return out


def _twin(tmp_path: Path, seed) -> Env:
    twin = Env(tmp_path, "twin")
    seed(twin)
    mcv.configure(twin.db, Settings(_env_file=str(twin.env_file)))
    return twin


def _outbound(env: Env, comment_id: str) -> dict[str, dict]:
    con = sqlite3.connect(env.db)
    con.row_factory = sqlite3.Row
    try:
        return {r["message_type"]: dict(r) for r in con.execute("SELECT * FROM outbound_messages WHERE source_id=?", (comment_id,))}
    finally:
        con.close()


def test_dm_then_public_path_writes_exactly_what_persist_success_wrote(env, tmp_path, capsys):
    def seed(e: Env) -> None:
        e.add_rule(keywords=("proxy",), dm_text=DM_TEXT)
        e.add_pending("instagram", "ig-eq", "proxy svp")

    seed(env)
    code, _lines, _err = env.run(capsys)
    assert code == 0
    sent = _outbound(env, "ig-eq")
    twin = _twin(tmp_path, seed)

    mcv.persist_success(
        platform="instagram", comment_id="ig-eq", keyword="proxy", media_id="media-1", dm_text=DM_TEXT,
        public_text=mcv.public_reply_for_platform("instagram", "C'est envoyé !"),
        dm_response={"message_id": sent["private_reply"]["meta_response_id"]},
        public_response={"id": sent["public_reply"]["meta_response_id"]},
    )

    assert _snapshot(env.db) == _snapshot(twin.db)


def test_follow_gate_path_writes_exactly_what_persist_follow_gate_pending_wrote(env, tmp_path, capsys):
    def seed(e: Env) -> None:
        e.add_rule("r-gate", platform="facebook", keywords=("gate",), dm_text=GATE_DM_TEXT, delivery_mode="follow_gate")
        e.add_pending("facebook", "fbeq", "gate please", media_id="post-1")

    seed(env)
    env.graph.resolve["fbeq"] = {"id": "fbeq", "message": "gate please", "from": {"id": "psid-e", "name": "Eve"}}
    code, _lines, _err = env.run(capsys)
    assert code == 0
    sent = _outbound(env, "fbeq")
    twin = _twin(tmp_path, seed)

    mcv.persist_follow_gate_pending(
        platform="facebook", comment_id="fbeq", sender_id="psid-e", campaign="r-gate", keyword="gate", media_id="post-1",
        dm_text=GATE_DM_TEXT, public_text=mcv.FOLLOW_GATE_PUBLIC_REPLY_TEXT,
        prompt_response={"message_id": sent["follow_gate_prompt"]["meta_response_id"]},
        public_response={"id": sent["public_reply"]["meta_response_id"]},
    )

    assert _snapshot(env.db) == _snapshot(twin.db)


def test_public_only_path_writes_exactly_what_persist_public_reply_only_wrote(env, tmp_path, capsys):
    def seed(e: Env) -> None:
        e.add_rule("r-public", keywords=("mcp",), dm_text=mcv.PUBLIC_REPLY_ONLY_DM_MARKER, public_reply_text="")
        e.add_pending("instagram", "ig-pubeq", "mcp")

    seed(env)
    code, _lines, _err = env.run(capsys)
    assert code == 0
    sent = _outbound(env, "ig-pubeq")
    twin = _twin(tmp_path, seed)

    mcv.persist_public_reply_only(
        platform="instagram", comment_id="ig-pubeq", keyword="mcp", media_id="media-1",
        public_text=mcv.varied_public_only_reply("mcp", "ig-pubeq"),
        public_response={"id": sent["public_reply"]["meta_response_id"]}, like_ok=True,
    )

    assert _snapshot(env.db) == _snapshot(twin.db)


# --------------------------------------------------------------------------- s13 (AC 7, m8)


def test_s13_console_disabled_rule_is_absent_from_load_rules(tmp_path):
    db = tmp_path / "db.sqlite3"
    store = CampaignRuleStore(db)
    store.upsert_rule(
        CampaignRule(
            source_task_id="console-0123456789ab", name="Console", platform="any", media_id=None,
            keywords=["voicebox"], public_reply_text="R", dm_text="D", enabled=True,
        )
    )
    mcv.configure(db, Settings(_env_file=None, meta_page_access_token=TOKEN))

    def toggle(enabled):
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE campaign_rules SET enabled = ?, source = 'console', updated_at = CURRENT_TIMESTAMP"
                " WHERE source_task_id = ?",
                (int(enabled), "console-0123456789ab"),
            )

    toggle(True)
    rules = mcv.load_rules()
    assert [r["source_task_id"] for r in rules] == ["console-0123456789ab"]
    rule, keyword = mcv.find_rule(platform="instagram", media_id="m1", text="la voicebox", rules=rules)
    assert rule is not None and keyword == "voicebox"

    toggle(False)
    rules = mcv.load_rules()
    assert rules == []
    assert mcv.find_rule(platform="instagram", media_id="m1", text="la voicebox", rules=rules) == (None, None)
