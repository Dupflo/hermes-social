import json

from app.cli import run


def test_cli_draft_is_local_only_and_requires_keyword(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    add = json.loads(run(["--db", str(db), "add-comment", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--comment-id", "c1", "--author", "@alice", "--text", "Proxy"]))
    assert add == {"ok": True, "changed": True}

    no_match = json.loads(run(["--db", str(db), "draft", "--comment-id", "c1", "--keyword", "système", "--reply", "x"]))
    assert no_match["error"] == "keyword_not_found"

    drafted = json.loads(run(["--db", str(db), "draft", "--comment-id", "c1", "--keyword", "proxy", "--reply", "Je te mets le lien ici 👍"]))
    assert drafted["ok"] is True
    assert drafted["mode"] == "draft_only"
    assert "nothing was posted" in drafted["message"]


def test_list_filters_status(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    run(["--db", str(db), "add-comment", "--video-url", "https://example.com/v", "--comment-id", "c1", "--author", "@a", "--text", "proxy"])
    run(["--db", str(db), "draft", "--comment-id", "c1", "--keyword", "proxy", "--reply", "ok"])

    result = json.loads(run(["--db", str(db), "list", "--status", "drafted_in_browser"]))

    assert result["ok"] is True
    assert [item["comment_id"] for item in result["items"]] == ["c1"]
