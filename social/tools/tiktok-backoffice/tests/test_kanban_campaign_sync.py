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
