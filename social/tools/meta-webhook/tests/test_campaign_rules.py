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
