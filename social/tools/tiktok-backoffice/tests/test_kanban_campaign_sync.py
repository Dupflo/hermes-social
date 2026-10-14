import json
import sqlite3

from app.cli import run


def _kanban_db(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                created_at INTEGER DEFAULT 1
            )
            """
        )
        connection.execute(
            "INSERT INTO tasks (id, title, body, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "task-proxy",
                "Campagne Proxy - EP9",
                """Statut métier: Actif
Plateforme: any
Mots-clés: proxy, proxi
Réponse publique: C'est envoyé, check tes DM
Message DM: Voici la ressource demandée : PRIVATE_RESOURCE
URL ressource: PRIVATE_RESOURCE
""",
                "ready",
                1,
            ),
        )
        connection.execute(
            "INSERT INTO tasks (id, title, body, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "task-template",
                "TEMPLATE - Campagne Meta",
                "Statut métier: Actif\nMots-clés: template\nMessage DM: nope",
                "ready",
                2,
            ),
        )
        connection.execute(
            "INSERT INTO tasks (id, title, body, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                "task-paused",
                "Campagne Pause",
                "Statut métier: Pause\nMots-clés: pause\nMessage DM: nope",
                "ready",
                3,
            ),
        )


def test_sync_kanban_campaigns_imports_active_resource_campaigns_safely(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    _kanban_db(kanban)

    result = json.loads(run(["--db", str(db), "sync-kanban-campaigns", "--kanban-db", str(kanban)]))

    assert result == {"ok": True, "synced": 1, "created": 1, "updated": 0, "skipped_console": 0}
    campaigns = json.loads(run(["--db", str(db), "list-campaigns"]))["items"]
    assert campaigns == [
        {
            "slug": "proxy",
            "name": "Campagne Proxy - EP9",
            "reply_template": "Je viens de t’envoyer le lien en message privé",
            "active": 1,
            "keywords": ["proxi", "proxy"],
        }
    ]


def test_sync_kanban_campaigns_then_suggests_video_mapping(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    _kanban_db(kanban)
    run(["--db", str(db), "add-video", "--video-url", "https://www.tiktok.com/@dupflodev/video/123", "--caption", "Commente proxy et je t envoie le repo"])

    run(["--db", str(db), "sync-kanban-campaigns", "--kanban-db", str(kanban)])
    result = json.loads(run(["--db", str(db), "suggest-video-campaigns"]))

    assert result == {"ok": True, "suggested": 1}


# --- s13: platform tiktok imported, console campaigns protected, s23 prerequisite, AC 7 ---

from app.kanban_import import parse_meta_campaign_card, sync_meta_campaigns_to_tiktok_report
from app.store import TikTokBackofficeStore
from s13_helpers import CONSOLE_SLUG, CONSOLE_VIDEO, dump_campaign, write_console_campaign

TIKTOK_CARD = """Statut métier: Actif
Plateforme: tiktok
Mots-clés: voicebox, voicebix
Message DM: Voici la ressource
"""


def _kanban_with(path, cards):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, status TEXT NOT NULL)")
        for task_id, title, body in cards:
            connection.execute("INSERT INTO tasks (id, title, body, status) VALUES (?, ?, ?, 'ready')", (task_id, title, body))


def test_tiktok_card_is_imported():
    campaign = parse_meta_campaign_card("task-tt", "Voicebox TikTok", TIKTOK_CARD)

    assert campaign is not None
    assert campaign.slug == "voicebox"
    assert campaign.keywords == ("voicebox", "voicebix")


def test_current_sync_against_console_campaigns(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    store = TikTokBackofficeStore(db)
    write_console_campaign(db)
    # A kanban campaign converted by the console: its computed slug is "voicebox".
    write_console_campaign(db, slug="voicebox", keywords=("voicebox", "voix"))
    before_console = dump_campaign(db, CONSOLE_SLUG)
    before_converted = dump_campaign(db, "voicebox")
    _kanban_with(kanban, [("task-tt", "Voicebox TikTok", TIKTOK_CARD)])

    report = sync_meta_campaigns_to_tiktok_report(store, kanban)

    assert report.skipped_console >= 1
    assert (report.created, report.updated, report.skipped_console) == (0, 0, 1)
    assert dump_campaign(db, CONSOLE_SLUG) == before_console
    assert dump_campaign(db, "voicebox") == before_converted


def test_console_campaign_is_a_poll_target_and_triggers_on_ingest(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    store = TikTokBackofficeStore(db)
    write_console_campaign(db)

    targets = store.poll_targets()
    assert [t["video_url"] for t in targets] == [CONSOLE_VIDEO]
    assert targets[0]["campaigns"] == [
        {
            "slug": CONSOLE_SLUG,
            "keywords": ["voicebix", "voicebox"],
            "reply_template": "Je viens de t’envoyer le lien en message privé",
        }
    ]

    result = store.ingest_comments(
        video_url=CONSOLE_VIDEO, comments=[{"id": "c1", "author": "@alice", "text": "je veux la voicebox"}]
    )
    assert result == {"ingested": 1, "created_reviews": 1}
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT campaign_slug FROM tiktok_review_items").fetchall() == [(CONSOLE_SLUG,)]


def test_disabled_console_campaign_stops_triggering(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    store = TikTokBackofficeStore(db)
    write_console_campaign(db, active=0)

    assert store.poll_targets() == []
    result = store.ingest_comments(
        video_url=CONSOLE_VIDEO, comments=[{"id": "c1", "author": "@alice", "text": "je veux la voicebox"}]
    )
    assert result["created_reviews"] == 0
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tiktok_review_items").fetchone() == (0,)


# --- s24 (console ADR 055, D1): the kanban sync stores the DM text of TikTok campaigns -----------------------------


def _card(keywords, dm=None, url=None):
    lines = ["Statut métier: Actif", "Plateforme: any", f"Mots-clés: {keywords}"]
    if dm is not None:
        lines.append(f"Message DM: {dm}")
    if url is not None:
        lines.append(f"URL ressource: {url}")
    return "\n".join(lines) + "\n"


def _dm_texts(db):
    with sqlite3.connect(db) as connection:
        return dict(connection.execute("SELECT slug, dm_text FROM tiktok_campaigns ORDER BY slug").fetchall())


def test_s24_colliding_slugs_keep_each_their_dm_text(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    _kanban_with(kanban, [("t1", "Proxy A", _card("proxy", "DM A")), ("t2", "Proxy B", _card("proxy, other", "DM B"))])
    sync_meta_campaigns_to_tiktok_report(TikTokBackofficeStore(db), kanban)
    assert _dm_texts(db) == {"proxy": "DM A", "proxy-t2": "DM B"}


def test_s24_multiline_dm_is_stored_with_its_newlines(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    body = "Statut métier: Actif\nMots-clés: proxy\nMessage DM: Salut !\nVoici le lien : http://ex.am/ple\nURL ressource: http://ex.am/ple\n"
    _kanban_with(kanban, [("t1", "Proxy", body)])
    sync_meta_campaigns_to_tiktok_report(TikTokBackofficeStore(db), kanban)
    assert _dm_texts(db) == {"proxy": "Salut !\nVoici le lien : http://ex.am/ple"}


def test_s24_card_with_only_a_resource_url_has_no_dm_text(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    _kanban_with(kanban, [("t1", "Proxy", _card("proxy", url="http://ex.am/ple"))])
    sync_meta_campaigns_to_tiktok_report(TikTokBackofficeStore(db), kanban)
    assert _dm_texts(db) == {"proxy": None}


def test_s24_console_campaign_dm_text_is_never_overwritten(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    store = TikTokBackofficeStore(db)
    write_console_campaign(db, slug="voicebox", keywords=("voicebox",))
    _kanban_with(kanban, [("t1", "Voicebox", _card("voicebox", "DM kanban"))])
    report = sync_meta_campaigns_to_tiktok_report(store, kanban)
    assert report.skipped_console == 1
    assert _dm_texts(db)["voicebox"] == "Le lien"


def test_s24_resync_is_idempotent_and_follows_the_card(tmp_path):
    db = tmp_path / "tiktok.sqlite3"
    kanban = tmp_path / "kanban.db"
    store = TikTokBackofficeStore(db)
    _kanban_with(kanban, [("t1", "Proxy", _card("proxy", "DM 1"))])
    sync_meta_campaigns_to_tiktok_report(store, kanban)
    sync_meta_campaigns_to_tiktok_report(store, kanban)
    assert _dm_texts(db) == {"proxy": "DM 1"}
    with sqlite3.connect(kanban) as connection:
        connection.execute("UPDATE tasks SET body = ? WHERE id = 't1'", (_card("proxy", "DM 2"),))
    sync_meta_campaigns_to_tiktok_report(store, kanban)
    assert _dm_texts(db) == {"proxy": "DM 2"}
    with sqlite3.connect(kanban) as connection:
        connection.execute("UPDATE tasks SET body = ? WHERE id = 't1'", (_card("proxy", url="http://x"),))
    sync_meta_campaigns_to_tiktok_report(store, kanban)
    assert _dm_texts(db) == {"proxy": None}


def test_s24_parsed_campaign_carries_dm_text():
    campaign = parse_meta_campaign_card("t1", "Proxy", _card("proxy", "  DM  "))
    assert campaign.dm_text == "DM"
