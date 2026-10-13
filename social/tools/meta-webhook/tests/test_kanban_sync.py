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


# --- s13: platform tiktok ignored, console rows protected, report counters ----------

from app.kanban_sync import main as kanban_sync_main, sync_kanban_campaigns_report
from app.campaign_rules import CampaignRule

ACTIVE_CARD = """
Statut métier: Actif
Plateforme: {platform}
Mots-clés: {keyword}
Réponse publique: C'est envoyé
Message DM: Le lien
"""


def _kanban(tmp_path, cards):
    kanban_db = tmp_path / "kanban.db"
    with sqlite3.connect(kanban_db) as connection:
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, status TEXT NOT NULL)")
        for task_id, title, body in cards:
            connection.execute(
                "INSERT INTO tasks (id, title, body, status) VALUES (?, ?, ?, 'ready')", (task_id, title, body)
            )
    return kanban_db


def _console_row(db_path, task_id):
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO campaign_rules (
                source_task_id, name, platform, media_id, keywords_json, public_reply_text, dm_text,
                enabled, delivery_mode, source, created_at, updated_at
            ) VALUES (?, 'Console', 'instagram', '17912345678901234', '["voicebox"]', 'R', 'D', 1,
                      'follow_gate', 'console', '2026-10-01 10:00:00', '2026-10-02 10:00:00')
            """,
            (task_id,),
        )


def _row(db_path, task_id):
    with sqlite3.connect(db_path) as connection:
        return connection.execute("SELECT * FROM campaign_rules WHERE source_task_id = ?", (task_id,)).fetchone()


def test_parse_campaign_card_ignores_tiktok_platform():
    body = ACTIVE_CARD.format(platform="tiktok", keyword="voicebox")

    assert parse_campaign_card("task-tt", "TikTok card", body) is None


def test_parse_campaign_card_keeps_any_for_other_unknown_platforms():
    rule = parse_campaign_card("task-x", "Threads", ACTIVE_CARD.format(platform="threads", keyword="voicebox"))

    assert rule is not None
    assert rule.platform == "any"


def test_sync_disables_previous_any_rule_born_from_a_tiktok_card(tmp_path):
    app_db = tmp_path / "app.sqlite3"
    store = CampaignRuleStore(app_db)
    store.upsert_rule(
        CampaignRule(
            source_task_id="task-tt", name="TikTok card", platform="any", media_id=None,
            keywords=["voicebox"], public_reply_text="R", dm_text="D", enabled=True,
        )
    )
    kanban_db = _kanban(tmp_path, [("task-tt", "TikTok card", ACTIVE_CARD.format(platform="tiktok", keyword="voicebox"))])

    sync_kanban_campaigns(kanban_db, app_db)

    rules = {rule.source_task_id: rule for rule in store.list_rules()}
    assert rules["task-tt"].enabled is False


def test_current_sync_against_a_console_campaign(tmp_path):
    app_db = tmp_path / "app.sqlite3"
    store = CampaignRuleStore(app_db)
    _console_row(app_db, "task-console")
    store.upsert_rule(
        CampaignRule(
            source_task_id="task-gone", name="Gone", platform="any", media_id=None,
            keywords=["gone"], public_reply_text="R", dm_text="D", enabled=True,
        )
    )
    before = _row(app_db, "task-console")
    kanban_db = _kanban(
        tmp_path,
        [
            ("task-console", "Same id", ACTIVE_CARD.format(platform="facebook", keyword="other")),
            ("task-new", "New", ACTIVE_CARD.format(platform="instagram", keyword="proxy")),
        ],
    )

    report = sync_kanban_campaigns_report(kanban_db, app_db)

    assert (report.created, report.updated, report.skipped_console) == (1, 0, 1)
    assert _row(app_db, "task-console") == before
    rules = {rule.source_task_id: rule for rule in store.list_rules()}
    assert rules["task-gone"].enabled is False
    assert rules["task-new"].enabled is True


def test_sync_kanban_campaigns_cli_prints_counters(tmp_path, monkeypatch, capsys):
    app_db = tmp_path / "app.sqlite3"
    CampaignRuleStore(app_db)
    _console_row(app_db, "task-console")
    kanban_db = _kanban(
        tmp_path,
        [
            ("task-console", "Same id", ACTIVE_CARD.format(platform="facebook", keyword="other")),
            ("task-new", "New", ACTIVE_CARD.format(platform="instagram", keyword="proxy")),
        ],
    )
    monkeypatch.setattr("sys.argv", ["kanban_sync", "--kanban-db", str(kanban_db), "--db", str(app_db)])

    kanban_sync_main()

    assert capsys.readouterr().out.strip() == "Synced 1 campaign rule(s) created=1 updated=0 skipped_console=1"


# --- s14: the report counts the rules the sync switched off (ADR 040 §2) -------------


def test_sync_report_counts_disabled_rules(tmp_path):
    app_db = tmp_path / "app.sqlite3"
    store = CampaignRuleStore(app_db)
    _console_row(app_db, "task-console")
    store.upsert_rule(
        CampaignRule(
            source_task_id="task-gone", name="Gone", platform="any", media_id=None,
            keywords=["gone"], public_reply_text="R", dm_text="D", enabled=True,
        )
    )
    kanban_db = _kanban(
        tmp_path,
        [
            ("task-console", "Same id", ACTIVE_CARD.format(platform="facebook", keyword="other")),
            ("task-new", "New", ACTIVE_CARD.format(platform="instagram", keyword="proxy")),
        ],
    )

    report = sync_kanban_campaigns_report(kanban_db, app_db)

    assert report.disabled == 1


def test_sync_report_second_identical_run_disables_nothing(tmp_path):
    app_db = tmp_path / "app.sqlite3"
    store = CampaignRuleStore(app_db)
    store.upsert_rule(
        CampaignRule(
            source_task_id="task-gone", name="Gone", platform="any", media_id=None,
            keywords=["gone"], public_reply_text="R", dm_text="D", enabled=True,
        )
    )
    kanban_db = _kanban(tmp_path, [("task-new", "New", ACTIVE_CARD.format(platform="instagram", keyword="proxy"))])

    first = sync_kanban_campaigns_report(kanban_db, app_db)
    second = sync_kanban_campaigns_report(kanban_db, app_db)

    assert first.disabled == 1
    assert second.disabled == 0
    assert (second.created, second.updated) == (0, 1)


def test_kanban_sync_report_disabled_defaults_to_zero():
    from app.kanban_sync import KanbanSyncReport

    assert KanbanSyncReport(created=1, updated=2, skipped_console=3).disabled == 0
