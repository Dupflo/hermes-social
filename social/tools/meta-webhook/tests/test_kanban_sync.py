import sqlite3

from app.kanban_sync import parse_campaign_card, sync_kanban_campaigns
from app.campaign_rules import CampaignRuleStore


def test_parse_campaign_card_extracts_fields():
    body = """
Statut métier: Actif
Plateforme: instagram
Media/Post ID: media-1
Mots-clés: proxy, repo, github
Réponse publique: C'est envoyé, check tes DM
Message DM: Voici le repo proxy : https://example.com/proxy
URL ressource: https://example.com/proxy
"""

    rule = parse_campaign_card("task-1", "Repo Proxy", body)

    assert rule is not None
    assert rule.source_task_id == "task-1"
    assert rule.name == "Repo Proxy"
    assert rule.platform == "instagram"
    assert rule.media_id == "media-1"
    assert rule.keywords == ["proxy", "repo", "github"]
    assert rule.public_reply_text == "C'est envoyé, check tes DM"
    assert rule.dm_text == "Voici le repo proxy : https://example.com/proxy"
    assert rule.enabled is True


def test_parse_campaign_card_uses_resource_url_when_dm_text_is_missing():
    body = """
Statut métier: Actif
Plateforme: instagram
Mots-clés: proxy
Réponse publique: C'est envoyé, check tes DM
URL ressource: https://example.com/proxy
"""

    rule = parse_campaign_card("task-1", "Repo Proxy", body)

    assert rule is not None
    assert rule.dm_text == "Voici la ressource demandée : https://example.com/proxy"


def test_parse_campaign_card_ignores_template_and_non_active_cards():
    template = """
Statut métier: Actif
Plateforme: instagram
Mots-clés: proxy
Réponse publique: Test
Message DM: Test
"""
    paused = template.replace("Actif", "Pause")

    assert parse_campaign_card("task-template", "TEMPLATE - Campagne Meta", template) is None
    assert parse_campaign_card("task-paused", "Proxy", paused) is None


def test_sync_kanban_campaigns_reads_active_tasks(tmp_path):
    kanban_db = tmp_path / "kanban.db"
    app_db = tmp_path / "app.sqlite3"
    with sqlite3.connect(kanban_db) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, status TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO tasks (id, title, body, status) VALUES (?, ?, ?, ?)",
            (
                "task-1",
                "Repo Proxy",
                """
Statut métier: Actif
Plateforme: instagram
Media/Post ID: media-1
Mots-clés: proxy
Réponse publique: C'est envoyé, check tes DM
Message DM: Voici le repo proxy : https://example.com/proxy
""",
                "ready",
            ),
        )

    synced = sync_kanban_campaigns(kanban_db, app_db)
    rules = CampaignRuleStore(app_db).list_rules()

    assert synced == 1
    assert len(rules) == 1
    assert rules[0].source_task_id == "task-1"
    assert rules[0].keywords == ["proxy"]
