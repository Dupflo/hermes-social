"""s24 — characterization of the legacy review processor before its replacement (console ADR 055, D10, AC 2).

The versioned processor (`scripts/process_tiktok_review_one.py`, byte-pinned by test_processor_versioned.py) is
loaded by path, never run as a script. Every side effect is replaced by a fake that journals the call:
`post` (Camofox HTTP), `eval_js` (page expressions), `shell` (docker exec / xclip / xdotool) and `time.sleep`.
`DB` and `KANBAN` point to temporary bases (real store + kanban fixture). One test per branch of `main()`
(l. 222-247): status, reason and call sequence.
"""

import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from app.models import Campaign
from app.store import TikTokBackofficeStore

PROCESSOR = Path(__file__).resolve().parents[1] / "scripts" / "process_tiktok_review_one.py"
VIDEO = "https://www.tiktok.com/@dupflodev/video/7000"
AUTHOR = "@someone"
COMMENT = "markitdown stp"
DM_TEXT = "Bonjour, tu reçois ce message car tu as commenté Markitdown : http://ex.am/ple"


def load_processor():
    spec = importlib.util.spec_from_file_location("process_tiktok_review_one", PROCESSOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Page:
    """Scripted Camofox: answers each page expression by the marker it contains, journals every call."""

    def __init__(self, **overrides):
        self.calls = []
        self.results = {
            "activate": {"clicked": None, "hasComments": True},
            "guards": {"ok": True, "stage": "guards_done", "alreadyReplied": False, "like": {"found": True}},
            "initial": {"hasLogin": False, "hasMessage": True, "title": "Someone (@someone) | TikTok"},
            "clicked": {"ok": True, "stage": "clicked_message"},
            "pre": {"editors": [{"i": 0, "text": ""}], "blocked": False, "alreadyMarkitdown": False},
            "focus": {"ok": True},
            "after_type": {"editors": [{"text": "http://ex.am/ple"}], "counterMatches": ["78/6000"], "sendCandidates": [{"i": 1}]},
            "send_click": {"ok": True},
            "after_send": {"editorEmpty": True, "sentTextVisible": True, "errorLike": False},
            "public": {"ok": True, "stage": "public_reply_posted_verified"},
        }
        self.results.update(overrides)
        self.shell_rc = 0

    MARKERS = (
        ("guards", "guards_done"),
        ("activate", "Read or add comments"),
        ("initial", "hasMessage:!!msg"),
        ("clicked", "clicked_message"),
        ("pre", "alreadyMarkitdown"),
        ("focus", "const el=editors[0]; el.focus()"),
        ("after_type", "counterMatches"),
        ("send_click", "no_send_candidate"),
        ("after_send", "sentTextVisible"),
        ("public", "public_reply_posted_verified"),
    )

    def post(self, path, payload, timeout=60, method="POST"):
        assert path == "/tabs"
        self.calls.append(("open_tab", payload["sessionKey"].split("_")[0], payload["url"]))
        return {"tabId": f"tab{len(self.calls)}"}

    def eval_js(self, tab, expr, timeout=120):
        for name, marker in self.MARKERS:
            if marker in expr:
                self.calls.append(("eval", name, "like" if "likedState" in expr else ""))
                return self.results[name]
        raise AssertionError("unexpected expression")

    def shell(self, cmd, timeout=60):
        kind = "clipboard" if "xclip" in cmd else "display"
        self.calls.append(("shell", kind, cmd))
        stdout = ":99\n" if kind == "display" else ""
        return subprocess.CompletedProcess(cmd, self.shell_rc if kind == "clipboard" else 0, stdout, "")

    def names(self):
        return [call[1] if call[0] != "open_tab" else f"tab:{call[1]}" for call in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    module = load_processor()
    store = TikTokBackofficeStore(tmp_path / "tiktok.sqlite3")
    for slug in ("markitdown", "systeme"):
        store.upsert_campaign(Campaign(slug=slug, name=slug, keywords=(slug,), reply_template="R"))
    with sqlite3.connect(store.database) as connection:
        connection.execute("INSERT INTO tiktok_videos (video_url, video_id) VALUES (?, '7000')", (VIDEO,))
        for i, slug in ((1, "markitdown"), (2, "systeme")):
            connection.execute(
                "INSERT INTO tiktok_comments (comment_id, video_url, author, text) VALUES (?, ?, ?, ?)",
                (f"c{i}", VIDEO, AUTHOR, COMMENT),
            )
            connection.execute(
                "INSERT INTO tiktok_review_items (id, comment_id, campaign_slug, matched_keyword, reply_text) VALUES (?, ?, ?, ?, 'R')",
                (i, f"c{i}", slug, slug),
            )
    kanban = tmp_path / "kanban.db"
    with sqlite3.connect(kanban) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT)")
        connection.execute(
            "INSERT INTO tasks VALUES ('t1', 'Campagne Markitdown - EP6', ?)",
            (f"Statut métier: actif\nMessage DM: {DM_TEXT}\nURL ressource: http://ex.am/ple\n",),
        )
    page = Page()
    monkeypatch.setattr(module, "DB", str(store.database))
    monkeypatch.setattr(module, "KANBAN", str(kanban))
    monkeypatch.setattr(module, "post", page.post)
    monkeypatch.setattr(module, "eval_js", page.eval_js)
    monkeypatch.setattr(module, "shell", page.shell)
    monkeypatch.setattr(module.time, "sleep", lambda s: page.calls.append(("sleep", "sleep", s)))

    class Env:
        pass

    e = Env()
    e.module, e.store, e.page, e.kanban = module, store, page, kanban

    def run(review_id):
        monkeypatch.setattr(module.sys, "argv", ["process_tiktok_review_one.py", str(review_id)])
        module.main()

    e.run = run
    return e


def status(env, review_id):
    with sqlite3.connect(env.store.database) as connection:
        return connection.execute(
            "SELECT status, failure_reason FROM tiktok_review_items WHERE id = ?", (review_id,)
        ).fetchone()


def events(env):
    with sqlite3.connect(env.store.database) as connection:
        return [row[0] for row in connection.execute("SELECT event_type FROM tiktok_browser_events ORDER BY id")]


def page_calls(env):
    return [n for n in env.page.names() if n != "sleep"]


def test_module_loads_without_side_effects(env):
    assert env.page.calls == []
    assert set(env.module.CAMPAIGN_TITLES) >= {"markitdown"}
    assert "systeme" not in env.module.CAMPAIGN_TITLES


def test_review_not_found_exits_without_any_call(env):
    with pytest.raises(SystemExit, match="review_not_found:99"):
        env.run(99)
    assert env.page.calls == []


def test_campaign_title_unknown_freezes_the_item(env):
    # The head of the production queue (`systeme`) is absent from CAMPAIGN_TITLES: status unchanged, no Camofox call.
    with pytest.raises(SystemExit, match="campaign_title_unknown:systeme"):
        env.run(2)
    assert status(env, 2) == ("pending_review", None)
    assert env.page.calls == []


def test_kanban_campaign_not_found(env):
    with sqlite3.connect(env.kanban) as connection:
        connection.execute("DELETE FROM tasks")
    with pytest.raises(SystemExit, match="kanban_campaign_not_found:Campagne Markitdown - EP6"):
        env.run(1)
    assert status(env, 1) == ("pending_review", None)
    assert env.page.calls == []


def test_dm_text_not_found(env):
    with sqlite3.connect(env.kanban) as connection:
        connection.execute("UPDATE tasks SET body = 'Statut métier: actif\nURL ressource: http://x'")
    with pytest.raises(SystemExit, match="dm_text_not_found"):
        env.run(1)
    assert status(env, 1) == ("pending_review", None)
    assert env.page.calls == []


def test_dm_text_is_read_by_the_message_dm_regex(env):
    assert env.module.get_dm_text("markitdown") == DM_TEXT


@pytest.mark.parametrize("stage", ["login_gate", "no_target"])
def test_failed_guard_writes_nothing(env, capsys, stage):
    env.page.results["guards"] = {"ok": False, "stage": stage}
    env.run(1)
    assert status(env, 1) == ("pending_review", None)
    assert events(env) == []
    assert page_calls(env) == ["tab:guard", "activate", "guards"]
    assert json.loads(capsys.readouterr().out)["stage"] == "guards_failed"


def test_creator_reply_already_seen_ignores_the_item(env):
    env.page.results["guards"] = {"ok": True, "stage": "guards_done", "alreadyReplied": True}
    env.run(1)
    assert status(env, 1) == ("ignored", "already_replied_by_creator_detected_before_dm")
    assert page_calls(env) == ["tab:guard", "activate", "guards"]
    assert events(env)[0].startswith("already_public_replied:review_id=1")


def test_like_is_emitted_before_any_dm(env):
    env.run(1)
    guard_calls = [c for c in env.page.calls if c[:2] == ("eval", "guards")]
    assert guard_calls == [("eval", "guards", "like")]
    names = page_calls(env)
    assert names.index("guards") < names.index("tab:dm")


def test_dm_goes_through_docker_exec_xclip_xdotool(env):
    env.run(1)
    clip = [c for c in env.page.calls if c[:2] == ("shell", "clipboard")]
    assert len(clip) == 1
    cmd = clip[0][2]
    assert cmd.startswith("docker exec hermes-social-camofox-1 ")
    assert "xclip -selection clipboard" in cmd and "xdotool key ctrl+v" in cmd


def test_nominal_sequence(env):
    env.run(1)
    assert page_calls(env) == [
        "tab:guard", "activate", "guards",
        "tab:dm", "initial", "clicked", "pre", "focus", "display", "clipboard", "after_type", "send_click", "after_send",
        "public",
    ]


@pytest.mark.parametrize(
    "override, stage",
    [
        ({"initial": {"hasLogin": True, "hasMessage": True}}, "profile_no_message_or_login"),
        ({"pre": {"editors": [], "blocked": False}}, "dm_blocked_or_no_editor"),
        ({"after_type": {"editors": [{"text": ""}], "counterMatches": ["0/6000"], "sendCandidates": [{"i": 1}]}}, "typed_but_send_not_ready"),
        ({"after_send": {"editorEmpty": True, "sentTextVisible": False, "errorLike": False}}, "sent_uncertain_or_failed"),
    ],
)
def test_unverified_dm_needs_manual_public_fallback(env, override, stage):
    env.page.results.update(override)
    env.run(1)
    assert status(env, 1) == ("needs_manual_public_fallback", f"dm_not_verified:{stage}")
    assert "public" not in page_calls(env)


def test_dm_verified_only_with_the_hardcoded_greeting(env):
    # after_send's verification looks for the fixed phrase 'Bonjour, tu reçois ce message' (l. 177).
    expr_seen = []
    real = env.page.eval_js

    def spy(tab, expr, timeout=120):
        if "sentTextVisible" in expr:
            expr_seen.append(expr)
        return real(tab, expr, timeout)

    env.module.eval_js = spy
    env.run(1)
    assert "Bonjour, tu reçois ce message" in expr_seen[0]


def test_dm_already_present_markitdown_is_ignored(env):
    env.page.results["pre"] = {"editors": [{"i": 0, "text": ""}], "blocked": False, "alreadyMarkitdown": True}
    env.run(1)
    st, reason = status(env, 1)
    assert st == "ignored"
    assert reason.startswith("already_dm_sent_detected_for_")
    assert "clipboard" not in page_calls(env)
    assert "public" not in page_calls(env)


def test_verified_dm_is_posted_even_when_public_reply_fails(env):
    env.page.results["public"] = {"ok": False, "stage": "no_reply_button"}
    env.run(1)
    st, reason = status(env, 1)
    assert st == "posted"
    assert reason.endswith("public_reply_stage=no_reply_button")


def test_public_reply_text_is_the_constant_not_the_stored_reply_text(env):
    seen = []
    real = env.page.eval_js

    def spy(tab, expr, timeout=120):
        if "public_reply_posted_verified" in expr:
            seen.append(expr)
        return real(tab, expr, timeout)

    env.module.eval_js = spy
    env.run(1)
    assert json.dumps(env.module.PUBLIC_REPLY) in seen[0]
    assert status(env, 1)[0] == "posted"
