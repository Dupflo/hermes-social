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

    assert result == {"ok": True, "synced": 1}
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
