from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.webhook_parser import CommentEvent


def test_find_matching_rule_prefers_media_specific_rule(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")
    store.upsert_rule(
        CampaignRule(
            source_task_id="global-proxy",
            name="Proxy global",
            platform="instagram",
            media_id=None,
            keywords=["proxy"],
            public_reply_text="Global reply",
            dm_text="Global DM",
            enabled=True,
        )
    )
    store.upsert_rule(
        CampaignRule(
            source_task_id="media-proxy",
            name="Proxy reel specific",
            platform="instagram",
            media_id="media-1",
            keywords=["proxy"],
            public_reply_text="Specific reply",
            dm_text="Specific DM",
            enabled=True,
        )
    )

    rule = store.find_matching_rule(CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy svp", media_id="media-1"))

    assert rule is not None
    assert rule.source_task_id == "media-proxy"
    assert rule.public_reply_text == "Specific reply"
    assert rule.dm_text == "Specific DM"


def test_find_matching_rule_accepts_comma_separated_media_ids(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")
    store.upsert_rule(
        CampaignRule(
            source_task_id="cross-platform-proxy",
            name="Proxy cross-platform",
            platform="any",
            media_id="facebook-post-1, instagram-media-1",
            keywords=["proxy"],
            public_reply_text="Reply",
            dm_text="DM",
            enabled=True,
        )
    )

    facebook_rule = store.find_matching_rule(
        CommentEvent(platform="facebook", comment_id="comment-1", text="Proxy", media_id="facebook-post-1")
    )
    instagram_rule = store.find_matching_rule(
        CommentEvent(platform="instagram", comment_id="comment-2", text="Proxy", media_id="instagram-media-1")
    )
    wrong_media_rule = store.find_matching_rule(
        CommentEvent(platform="facebook", comment_id="comment-3", text="Proxy", media_id="other-post")
    )

    assert facebook_rule is not None
    assert instagram_rule is not None
    assert wrong_media_rule is None


def test_find_matching_rule_ignores_disabled_and_non_matching_keywords(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")
    store.upsert_rule(
        CampaignRule(
            source_task_id="disabled-proxy",
            name="Proxy disabled",
            platform="instagram",
            media_id=None,
            keywords=["proxy"],
            public_reply_text="Reply",
            dm_text="DM",
            enabled=False,
        )
    )

    assert store.find_matching_rule(CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy")) is None
    assert store.find_matching_rule(CommentEvent(platform="instagram", comment_id="comment-2", text="Hello")) is None


def test_has_enabled_rules_tracks_active_campaigns(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")
    assert store.has_enabled_rules() is False

    store.upsert_rule(
        CampaignRule(
            source_task_id="disabled",
            name="Disabled",
            platform="any",
            media_id="media-1",
            keywords=["proxy"],
            public_reply_text="Reply",
            dm_text="DM",
            enabled=False,
        )
    )
    assert store.has_enabled_rules() is False

    store.upsert_rule(
        CampaignRule(
            source_task_id="enabled",
            name="Enabled",
            platform="any",
            media_id="media-1",
            keywords=["proxy"],
            public_reply_text="Reply",
            dm_text="DM",
            enabled=True,
        )
    )
    assert store.has_enabled_rules() is True


def test_upsert_rule_updates_existing_task_rule(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")
    store.upsert_rule(
        CampaignRule(
            source_task_id="task-1",
            name="Old",
            platform="any",
            media_id=None,
            keywords=["proxy"],
            public_reply_text="Old reply",
            dm_text="Old DM",
            enabled=True,
        )
    )
    store.upsert_rule(
        CampaignRule(
            source_task_id="task-1",
            name="New",
            platform="any",
            media_id=None,
            keywords=["vps", "proxy"],
            public_reply_text="New reply",
            dm_text="New DM",
            enabled=True,
        )
    )

    rules = store.list_rules()

    assert len(rules) == 1
    assert rules[0].name == "New"
    assert rules[0].keywords == ["vps", "proxy"]


def test_disable_rules_except_disables_stale_synced_rules(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")
    store.upsert_rule(
        CampaignRule(
            source_task_id="keep",
            name="Keep",
            platform="any",
            media_id=None,
            keywords=["proxy"],
            public_reply_text="Reply",
            dm_text="DM",
            enabled=True,
        )
    )
    store.upsert_rule(
        CampaignRule(
            source_task_id="stale",
            name="Stale",
            platform="any",
            media_id=None,
            keywords=["magic"],
            public_reply_text="Reply",
            dm_text="DM",
            enabled=True,
        )
    )

    store.disable_rules_except({"keep"})

    rules = {rule.source_task_id: rule for rule in store.list_rules()}
    assert rules["keep"].enabled is True
    assert rules["stale"].enabled is False


# --- s13: source column, protected sync, has_any_rule -------------------------------

import sqlite3

PRE_S13_DDL = """
CREATE TABLE campaign_rules (
    source_task_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    media_id TEXT,
    keywords_json TEXT NOT NULL,
    public_reply_text TEXT NOT NULL,
    dm_text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    delivery_mode TEXT NOT NULL DEFAULT 'direct',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _kanban_rule(task_id: str, **overrides) -> CampaignRule:
    values = dict(
        source_task_id=task_id,
        name=f"Rule {task_id}",
        platform="any",
        media_id=None,
        keywords=["proxy"],
        public_reply_text="Reply",
        dm_text="DM",
        enabled=True,
    )
    values.update(overrides)
    return CampaignRule(**values)


def _insert_console_row(db_path, task_id: str = "console-0123456789ab", enabled: int = 0) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO campaign_rules (
                source_task_id, name, platform, media_id, keywords_json,
                public_reply_text, dm_text, enabled, delivery_mode, source,
                created_at, updated_at
            ) VALUES (?, 'Console', 'any', '17912345678901234,1234567890_9876543210',
                      '["voicebox","voicebix"]', 'A\n---VARIANT---\nB', 'Le lien', ?,
                      'follow_gate', 'console', '2026-10-01 10:00:00', '2026-10-02 10:00:00')
            """,
            (task_id, enabled),
        )


def _dump(db_path, task_id: str):
    with sqlite3.connect(db_path) as connection:
        return connection.execute("SELECT * FROM campaign_rules WHERE source_task_id = ?", (task_id,)).fetchone()


def test_ensure_schema_adds_source_column_to_pre_s13_table(tmp_path):
    db = tmp_path / "campaigns.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute(PRE_S13_DDL)
        connection.execute(
            "INSERT INTO campaign_rules (source_task_id, name, platform, keywords_json, public_reply_text, dm_text)"
            " VALUES ('task-1', 'Old', 'any', '[\"proxy\"]', 'R', 'D')"
        )

    store = CampaignRuleStore(db)
    CampaignRuleStore(db)  # idempotent: second construction does not fail

    with sqlite3.connect(db) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(campaign_rules)")]
        sources = connection.execute("SELECT source FROM campaign_rules").fetchall()
    assert columns.count("source") == 1
    assert sources == [("kanban",)]
    assert store.list_rules()[0].source == "kanban"


def test_upsert_rule_reports_created_then_updated(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")

    assert store.upsert_rule(_kanban_rule("task-1")) == "created"
    assert store.upsert_rule(_kanban_rule("task-1", name="Renamed")) == "updated"
    assert store.list_rules()[0].name == "Renamed"


def test_upsert_rule_skips_console_row_and_leaves_it_identical(tmp_path):
    db = tmp_path / "campaigns.sqlite3"
    store = CampaignRuleStore(db)
    _insert_console_row(db)
    before = _dump(db, "console-0123456789ab")

    result = store.upsert_rule(
        _kanban_rule("console-0123456789ab", name="From kanban", keywords=["other"], delivery_mode="direct")
    )

    assert result == "skipped_console"
    assert _dump(db, "console-0123456789ab") == before


def test_disable_rules_except_never_touches_console_rows(tmp_path):
    db = tmp_path / "campaigns.sqlite3"
    store = CampaignRuleStore(db)
    store.upsert_rule(_kanban_rule("kanban-1"))
    _insert_console_row(db, enabled=1)
    before = _dump(db, "console-0123456789ab")

    store.disable_rules_except({"autre"})

    rules = {rule.source_task_id: rule for rule in store.list_rules()}
    assert rules["kanban-1"].enabled is False
    assert rules["console-0123456789ab"].enabled is True
    assert _dump(db, "console-0123456789ab") == before


def test_disable_rules_except_empty_set_never_touches_console_rows(tmp_path):
    db = tmp_path / "campaigns.sqlite3"
    store = CampaignRuleStore(db)
    store.upsert_rule(_kanban_rule("kanban-1"))
    _insert_console_row(db, enabled=1)
    before = _dump(db, "console-0123456789ab")

    store.disable_rules_except(set())

    rules = {rule.source_task_id: rule for rule in store.list_rules()}
    assert rules["kanban-1"].enabled is False
    assert rules["console-0123456789ab"].enabled is True
    assert _dump(db, "console-0123456789ab") == before


def test_has_any_rule_counts_disabled_rows(tmp_path):
    store = CampaignRuleStore(tmp_path / "campaigns.sqlite3")
    assert store.has_any_rule() is False

    store.upsert_rule(_kanban_rule("disabled", enabled=False))

    assert store.has_any_rule() is True
    assert store.has_enabled_rules() is False
