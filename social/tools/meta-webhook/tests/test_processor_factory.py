"""s09 — the webhook and the replay build their CommentProcessor the same way."""

from app.config import Settings
from app.follow_gate_store import FollowGateStore
from app.outbound_message_store import OutboundMessageStore
from app.campaign_rules import CampaignRuleStore
from app.processor import CommentProcessor
from app.processor_factory import build_comment_processor
from app.store import ProcessedCommentStore


def _settings(tmp_path, **overrides):
    values = {
        "processed_comments_database": str(tmp_path / "processed.sqlite3"),
        "meta_page_id": "page-1",
        "meta_ig_user_id": "ig-1",
        "meta_owner_ids": "owner-9",
        "meta_owner_usernames": "dupflodev",
        "resource_keyword": "proxy",
        "resource_url": "https://example.com/r",
        "public_reply_text": "C'est envoyé",
        "instagram_app_secret": "ig-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_factory_builds_the_webhook_processor(tmp_path):
    graph = object()
    processor = build_comment_processor(_settings(tmp_path), graph)

    assert isinstance(processor, CommentProcessor)
    assert processor.graph_client is graph
    assert isinstance(processor.store, ProcessedCommentStore)
    assert isinstance(processor.outbound_store, OutboundMessageStore)
    assert isinstance(processor.rule_store, CampaignRuleStore)
    assert isinstance(processor.follow_gate_store, FollowGateStore)
    for store in (processor.store, processor.outbound_store, processor.rule_store, processor.follow_gate_store):
        assert str(store.database_path) == str(tmp_path / "processed.sqlite3")
    assert processor.page_id == "page-1"
    assert processor.keyword == "proxy"
    assert processor.resource_url == "https://example.com/r"
    assert processor.public_reply_text == "C'est envoyé"
    assert processor.owner_ids == {"owner-9", "page-1", "ig-1"}
    assert processor.owner_usernames == {"dupflodev"}
    assert processor.instagram_messaging_enabled is True


def test_factory_disables_instagram_messaging_without_the_instagram_secret(tmp_path):
    processor = build_comment_processor(_settings(tmp_path, instagram_app_secret=""), object())
    assert processor.instagram_messaging_enabled is False
