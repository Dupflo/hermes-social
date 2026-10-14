import json

from app import cli
from app.cli import run


def _setup_video_campaign(db):
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Commente proxy"])
    run(["--db", str(db), "campaign-upsert", "--slug", "proxy", "--name", "Proxy", "--keywords", "proxy", "--reply", "Je viens de t’envoyer le lien en message privé"])
    run(["--db", str(db), "assign-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--campaign", "proxy", "--source", "manual"])


def test_extract_dom_comments_filters_recommendations_and_navigation():
    from app.camofox_reader import extract_comments_from_dom_result

    result = {
        "url": "https://www.tiktok.com/@dupflodev/video/123",
        "logged_in": True,
        "comment_nodes": [
            {"author": "@alice", "text": "proxy stp", "comment_id": "c1"},
            {"author": "TikTok", "text": "Search"},
            {"author": "@suggested", "text": "You may like"},
            {"author": "@bob", "text": "bonjour", "comment_id": "c2"},
            {"author": "@alice", "text": "proxy stp", "comment_id": "c1"},
        ],
    }

    comments = extract_comments_from_dom_result(result)

    assert comments == [
        {"id": "c1", "author": "@alice", "text": "proxy stp"},
        {"id": "c2", "author": "@bob", "text": "bonjour"},
    ]



def test_extract_dom_comments_filters_related_tab_recommendation_cards():
    from app.camofox_reader import extract_comments_from_dom_result

    result = {
        "url": "https://www.tiktok.com/@dupflodev/video/123",
        "logged_in": False,
        "related_tab_active": True,
        "comment_nodes": [
            {
                "author": "@dupflodev",
                "text": "Un repo GitHub compile les system prompts leakés de Claude, ChatGPT, Cursor, v0. Et partout le même commentaire : plus besoin de payer.",
                "comment_id": "css-649dsf-5e6d46e3--DivInfoContainer e9pwkrg3",
            },
            {"author": "@alice", "text": "proxy stp", "comment_id": "real-c1"},
        ],
    }

    comments = extract_comments_from_dom_result(result)

    assert comments == [{"id": "real-c1", "author": "@alice", "text": "proxy stp"}]


def test_fetch_comments_camofox_cli_can_ingest_without_publishing(tmp_path, monkeypatch):
    db = tmp_path / "tiktok.sqlite3"
    _setup_video_campaign(db)

    def fake_fetch(**kwargs):
        assert kwargs["video_url"] == "https://www.tiktok.com/@dupflodev/video/123"
        return {
            "ok": True,
            "video_url": kwargs["video_url"],
            "logged_in": True,
            "comments": [
                {"id": "c1", "author": "@alice", "text": "proxy"},
                {"id": "c2", "author": "@bob", "text": "hors sujet"},
            ],
            "diagnostics": {"comment_nodes": 2},
        }

    monkeypatch.setattr(cli, "fetch_comments_from_camofox", fake_fetch)

    result = json.loads(run([
        "--db", str(db),
        "fetch-comments-camofox",
        "--video-url", "https://www.tiktok.com/@dupflodev/video/123",
        "--ingest",
    ]))

    assert result["ok"] is True
    assert result["fetched"] == 2
    assert result["ingest"] == {"ingested": 2, "created_reviews": 1}
    item = json.loads(run(["--db", str(db), "next-review"]))["item"]
    assert item["comment_id"] == "c1"
    assert item["reply_text"] == "Je viens de t’envoyer le lien en message privé"


def test_fetch_comments_camofox_cli_reports_fetch_errors(tmp_path, monkeypatch):
    db = tmp_path / "tiktok.sqlite3"

    def fake_fetch(**kwargs):
        return {"ok": False, "error": "camofox_unreachable", "comments": []}

    monkeypatch.setattr(cli, "fetch_comments_from_camofox", fake_fetch)

    result = json.loads(run([
        "--db", str(db),
        "fetch-comments-camofox",
        "--video-url", "https://www.tiktok.com/@dupflodev/video/123",
    ]))

    assert result == {"ok": False, "error": "camofox_unreachable", "comments": [], "fetched": 0}


def test_camofox_logged_in_detection_rejects_log_in_to_comment():
    # s26 : épingle amendée (garde structurelle readerGuard/loginGate au lieu d'une regex du corps, console ADR 060)
    # — l'intention (« Log in to comment » n'est pas connecté) passe à tests/js/reader_guard.test.mjs.
    from app.camofox_reader import _COMMENT_EXTRACTION_JS

    assert "logged_in" in _COMMENT_EXTRACTION_JS
    assert "loginGate()" in _COMMENT_EXTRACTION_JS
    assert "Log\\s*in" not in _COMMENT_EXTRACTION_JS
    assert "to\\s+comment" not in _COMMENT_EXTRACTION_JS
    assert "Sign\\s*up" not in _COMMENT_EXTRACTION_JS
    assert "text.includes('Messages')" not in _COMMENT_EXTRACTION_JS


def test_reader_guard_js_is_passive():
    from app.camofox_reader import _READER_GUARD_JS

    for forbidden in ("click(", "dispatchEvent", "scrollIntoView", "location", "reload", "navigate"):
        assert forbidden not in _READER_GUARD_JS, forbidden


def test_reader_guard_js_reuses_the_login_gate_of_the_reply_selectors():
    from app import tiktok_reply_selectors as sel
    from app.camofox_reader import _READER_GUARD_JS

    gate = [line for line in sel._HELPERS.splitlines() if line.startswith(" function loginGate(")]
    assert len(gate) == 1
    assert gate[0] in _READER_GUARD_JS.splitlines()
    assert "return readerGuard()" in _READER_GUARD_JS


def test_extraction_logged_in_comes_from_the_reader_guard():
    from app.camofox_reader import _COMMENT_EXTRACTION_JS

    assert "logged_in: g.state === 'ok'" in _COMMENT_EXTRACTION_JS
    assert "login_gate: g.login_gate" in _COMMENT_EXTRACTION_JS
    assert "reader_guard: g" in _COMMENT_EXTRACTION_JS
    # the nested IIFE keeps its own helpers: no second top-level `clean` of the extraction scope
    assert _COMMENT_EXTRACTION_JS.count("function clean(s) {") == 1


def test_coherence_login_gate_comes_from_the_dom_flag_not_a_body_regex():
    from app.camofox_reader import _target_video_coherence

    url = "https://www.tiktok.com/@dupflodev/video/123"
    dom = {"url": url, "logged_in": False, "login_gate": False, "body_preview": "Log in"}
    assert _target_video_coherence(url, dom, [])["login_gate"] is False
    dom = {"url": url, "logged_in": False, "login_gate": True, "body_preview": ""}
    assert _target_video_coherence(url, dom, [])["login_gate"] is True


def _js_selector_list(literal: str) -> list[str]:
    import re

    return re.findall(r"'([^']+)'", literal)


def test_reader_guard_items_are_exactly_the_extraction_containers():
    import re

    from app.camofox_reader import _COMMENT_EXTRACTION_JS, _READER_GUARD_FN

    block = re.search(r"const containerSelector = \[(.*?)\]\.join", _COMMENT_EXTRACTION_JS, re.S)
    assert block
    extraction = _js_selector_list(block.group(1))
    items = re.search(r"const itemSel='([^']+)'", _READER_GUARD_FN)
    assert items
    assert items.group(1).split(",") == extraction


def test_reader_guard_states_closed_list():
    from app.camofox_reader import READER_GUARD_STATES

    assert READER_GUARD_STATES == ("ok", "login_gate", "no_signal")



def test_extract_dom_comments_falls_back_to_visible_comments_panel_text():
    from app.camofox_reader import extract_comments_from_dom_result

    result = {
        "comment_nodes": [],
        "active_right_panel_preview": """
Comments
You may like
13 comments
stdn
higgsfield
4d ago
Reply
1
Ashvin Appigadoo
Higgsfield
7-18
Reply
1
View 1 reply
loki_le_fripon
Higgsfield 🔥
7-8
Reply
1
View 1 reply
Elsa
higgsfield
7-5
Reply
1
View 1 reply
Add comment...
""",
    }

    comments = extract_comments_from_dom_result(result)

    assert comments[:4] == [
        {"author": "stdn", "text": "higgsfield"},
        {"author": "Ashvin Appigadoo", "text": "Higgsfield"},
        {"author": "loki_le_fripon", "text": "Higgsfield 🔥"},
        {"author": "Elsa", "text": "higgsfield"},
    ]


def test_activate_comments_tab_prefers_comment_icon_not_favorites_or_bookmark():
    from app.camofox_reader import _ACTIVATE_COMMENTS_TAB_JS

    assert 'data-e2e="comment-icon"' in _ACTIVATE_COMMENTS_TAB_JS
    assert "comment_icon" in _ACTIVATE_COMMENTS_TAB_JS
    assert "favorite-icon" not in _ACTIVATE_COMMENTS_TAB_JS
    assert "BookmarkWrapper" not in _ACTIVATE_COMMENTS_TAB_JS



def test_extract_dom_comments_does_not_parse_login_gate_as_comments():
    from app.camofox_reader import extract_comments_from_dom_result

    result = {
        "logged_in": False,
        "comment_nodes": [],
        "body_preview": """
TikTok
Search
Profile
More
Log in
We're having trouble playing this video. Please refresh and try again.
42
13
dupflodev
· 1d ago
Tu passes de Claude Code à Codex ? Commente MIGRATION 👇
""",
    }

    assert extract_comments_from_dom_result(result) == []



def test_target_video_coherence_rejects_recommended_video_dom_under_target_url():
    from app.camofox_reader import _target_video_coherence

    dom_result = {
        "url": "https://www.tiktok.com/@dupflodev/video/7657941429938949399",
        "title": "(26)🪐 J’ai ajouté ce qui manquait : de vrais assets 3D. Et là, ça change ... | TikTok",
        "logged_in": True,
        "body_preview": """
TikTok
Messages
We're having trouble playing this video. Please refresh and try again.
Comments
You may like
#finalworldcup #spainvsfrance
princesleono8
1.2M
""",
        "active_right_panel_preview": "princesleono8 1.2M · 6d ago #finalworldcup",
    }

    coherence = _target_video_coherence(
        "https://www.tiktok.com/@dupflodev/video/7657941429938949399",
        dom_result,
        [],
    )

    assert coherence["coherent"] is False
    assert coherence["reason"] == "target_author_missing_from_loaded_body"
    assert coherence["loaded_has_video_id"] is True
    assert coherence["body_has_author"] is False


def test_target_video_coherence_accepts_target_author_in_body():
    from app.camofox_reader import _target_video_coherence

    dom_result = {
        "url": "https://www.tiktok.com/@dupflodev/video/7657941429938949399",
        "title": "Higgsfield | TikTok",
        "logged_in": True,
        "body_preview": "dupflodev · 7-2 Commente HIGGSFIELD",
        "active_right_panel_preview": "13 comments Elsa higgsfield",
    }

    coherence = _target_video_coherence(
        "https://www.tiktok.com/@dupflodev/video/7657941429938949399",
        dom_result,
        [],
    )

    assert coherence["coherent"] is True
    assert coherence["body_has_author"] is True


def test_target_video_coherence_accepts_target_author_from_extracted_comments():
    from app.camofox_reader import _target_video_coherence

    dom_result = {
        "url": "https://www.tiktok.com/@dupflodev/video/7657941429938949399",
        "title": "Higgsfield | TikTok",
        "logged_in": True,
        "body_preview": "TikTok Messages Comments",
        "active_right_panel_preview": "13 comments",
    }

    coherence = _target_video_coherence(
        "https://www.tiktok.com/@dupflodev/video/7657941429938949399",
        dom_result,
        [{"author": "@dupflodev", "text": "Creator reply"}],
    )

    assert coherence["coherent"] is True
    assert coherence["comments_have_author"] is True


def test_target_video_coherence_rejects_silent_jump_to_recommended_video():
    from app.camofox_reader import _target_video_coherence

    result = _target_video_coherence(
        "https://www.tiktok.com/@dupflodev/video/7667537436582464801",
        {
            "url": "https://www.tiktok.com/@niko_bellic_gta_4/video/7650502123054042401",
            "title": "Niko Nailed the Landing | TikTok",
            "logged_in": True,
            "body_preview": "Comments You may like dupflodev 3d ago",
            "active_right_panel_preview": "Comments You may like dupflodev 3d ago",
        },
        [],
    )

    assert result["coherent"] is False
    assert result["reason"] == "loaded_different_video"


def test_comment_count_guard_detects_expected_comments_without_rows():
    from app.camofox_reader import _expected_comment_count_from_activation, _right_panel_looks_like_recommendations

    activation = {"comment_icon_candidates": [{"text": "2", "aria": "Read or add comments\n2 comments"}]}
    dom = {"active_right_panel_preview": "Comments You may like funny #fyp user 1d ago"}

    assert _expected_comment_count_from_activation(activation) == 2
    assert _right_panel_looks_like_recommendations(dom) is True
