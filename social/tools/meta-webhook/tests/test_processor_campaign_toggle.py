"""s13 (AC 7, ADR 039 §7) — disabling a campaign stops it at once, and disabling the last one
never falls back to the legacy .env campaign.

One processor, built by the webhook/replay factory, is used across the whole sequence: it proves
no cache keeps a rule alive. The toggle is the exact UPDATE the console runs (P9).
"""

import sqlite3

import pytest

from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.config import Settings
from app.processor_factory import build_comment_processor
from app.webhook_parser import CommentEvent


class RecordingGraph:
    def __init__(self):
        self.calls = []

    async def like_comment(self, comment_id):
        self.calls.append(("like", comment_id))
        return {"success": True}

    async def reply_to_instagram_comment(self, comment_id, message):
        self.calls.append(("public_reply", comment_id, message))
        return {"id": "reply-id"}

    async def private_reply_to_instagram_comment(self, page_id, comment_id, text, quick_replies=None):
        self.calls.append(("private_reply", page_id, comment_id, text))
        return {"message_id": "message-id"}

    async def send_direct_message(self, recipient_id, text, quick_replies=None):
        self.calls.append(("direct_message", recipient_id, text))
        return {"message_id": "direct-id"}

    async def reply_to_facebook_comment(self, comment_id, message):
        self.calls.append(("facebook_public_reply", comment_id, message))
        return {"id": "reply-id"}

    async def private_reply_to_facebook_comment(self, page_id, comment_id, text):
        self.calls.append(("facebook_private_reply", page_id, comment_id, text))
        return {"id": "message-id"}

    async def can_reply_privately_to_facebook_comment(self, comment_id):
        return True


def _settings(db):
    return Settings(
        _env_file=None,
        processed_comments_database=str(db),
        meta_page_id="page-1",
        meta_ig_user_id="ig-1",
        meta_owner_ids="",
        meta_owner_usernames="dupflodev",
        resource_keyword="proxy",
        resource_url="https://example.com/r",
        public_reply_text="C'est envoyé",
    )


def _rule(task_id, keyword):
    return CampaignRule(
        source_task_id=task_id,
        name=task_id,
        platform="any",
        media_id=None,
        keywords=[keyword],
        public_reply_text="Reply",
        dm_text="DM",
        enabled=True,
    )


def _console_toggle(db, task_id, enabled):
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE campaign_rules SET enabled = ?, source = 'console', updated_at = CURRENT_TIMESTAMP"
            " WHERE source_task_id = ?",
            (int(enabled), task_id),
        )


def _processed_count(db):
    with sqlite3.connect(db) as connection:
        return connection.execute("SELECT COUNT(*) FROM processed_comments").fetchone()[0]


def _event(comment_id, text):
    return CommentEvent(platform="instagram", comment_id=comment_id, text=text, media_id="media-1", author_id=f"user-{comment_id}", username=f"user{comment_id}")


@pytest.mark.asyncio
async def test_disabling_stops_new_processing_and_never_falls_back_to_env(tmp_path):
    db = tmp_path / "processed.sqlite3"
    store = CampaignRuleStore(db)
    store.upsert_rule(_rule("X", "voicebox"))
    store.upsert_rule(_rule("Y", "proxy"))
    graph = RecordingGraph()
    processor = build_comment_processor(_settings(db), graph)

    # (a) both enabled: X fires
    [result] = await processor.process_events([_event("c1", "je veux la voicebox")])
    assert result.status == "processed"
    assert graph.calls
    calls_after_a = list(graph.calls)
    processed_after_a = _processed_count(db)

    # (b) X disabled by the console: ignored, no Graph call, nothing claimed
    _console_toggle(db, "X", False)
    [result] = await processor.process_events([_event("c2", "voicebox")])
    assert result.status == "ignored"
    assert graph.calls == calls_after_a
    assert _processed_count(db) == processed_after_a

    # (c) Y disabled too (no enabled rule left): "proxy" (= RESOURCE_KEYWORD) is ignored, no fallback
    _console_toggle(db, "Y", False)
    [result] = await processor.process_events([_event("c3", "proxy")])
    assert result.status == "ignored"
    assert graph.calls == calls_after_a
    assert _processed_count(db) == processed_after_a

    # (d) X re-enabled: fires again
    _console_toggle(db, "X", True)
    [result] = await processor.process_events([_event("c4", "voicebox stp")])
    assert result.status == "processed"


@pytest.mark.asyncio
async def test_empty_campaign_table_keeps_the_env_fallback(tmp_path):
    db = tmp_path / "processed.sqlite3"
    graph = RecordingGraph()
    processor = build_comment_processor(_settings(db), graph)

    [result] = await processor.process_events([_event("c1", "proxy")])

    assert result.status == "processed"
    assert graph.calls
