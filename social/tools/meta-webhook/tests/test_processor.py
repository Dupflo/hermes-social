import pytest

from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.follow_gate_store import FollowGateStore
from app.follow_gate_store import FOLLOW_GATE_CONFIRMATION_PAYLOAD
from app.processor import PrivateMessageProcessor
from app.graph_client import GraphAPIError
from app.outbound_message_store import OutboundMessageStore
from app.processor import CommentProcessor, ProcessingResult
from app.store import ProcessedCommentStore
from app.webhook_parser import CommentEvent
from app.webhook_parser import PrivateMessageEvent


def age_processed_rows(database, minutes=10):
    """Simulate a checkpoint left by a run that finished long ago."""
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE processed_comments SET processed_at = datetime('now', ?)",
            (f"-{minutes} minutes",),
        )


class FakeGraphClient:
    def __init__(self):
        self.calls = []

    async def like_comment(self, comment_id: str):
        self.calls.append(("like", comment_id))
        return {"success": True}

    async def reply_to_instagram_comment(self, comment_id: str, message: str):
        self.calls.append(("public_reply", comment_id, message))
        return {"id": "reply-id"}

    async def private_reply_to_instagram_comment(self, page_id: str, comment_id: str, text: str, quick_replies=None):
        self.calls.append(("private_reply", page_id, comment_id, text, quick_replies))
        return {"message_id": "message-id"}

    async def send_direct_message(self, recipient_id: str, text: str, quick_replies=None):
        self.calls.append(("direct_message", recipient_id, text, quick_replies))
        return {"message_id": "direct-message-id"}

    async def reply_to_facebook_comment(self, comment_id: str, message: str):
        self.calls.append(("facebook_public_reply", comment_id, message))
        return {"id": "reply-id"}

    async def private_reply_to_facebook_comment(self, page_id: str, comment_id: str, text: str):
        self.calls.append(("facebook_private_reply", page_id, comment_id, text))
        return {"id": "message-id"}

    async def can_reply_privately_to_facebook_comment(self, comment_id: str) -> bool:
        self.calls.append(("facebook_can_reply_privately", comment_id))
        return True


class FailingGraphClient(FakeGraphClient):
    async def reply_to_instagram_comment(self, comment_id: str, message: str):
        self.calls.append(("public_reply", comment_id, message))
        raise GraphAPIError("Meta Graph API error 190: expired token")


class LikeFailingGraphClient(FakeGraphClient):
    async def like_comment(self, comment_id: str):
        self.calls.append(("like", comment_id))
        raise GraphAPIError("Meta Graph API error 100: unsupported like")


class FacebookPrivateReplyBlockedGraphClient(FakeGraphClient):
    async def can_reply_privately_to_facebook_comment(self, comment_id: str) -> bool:
        self.calls.append(("facebook_can_reply_privately", comment_id))
        return False


class InstagramPrivateReplyBlockedGraphClient(FakeGraphClient):
    async def private_reply_to_instagram_comment(self, page_id: str, comment_id: str, text: str, quick_replies=None):
        self.calls.append(("private_reply", page_id, comment_id, text, quick_replies))
        raise GraphAPIError("Meta Graph API error 10901: Activity replying time expired", code=10901)


@pytest.mark.asyncio
async def test_processor_handles_matching_instagram_comment_once(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(
        platform="instagram",
        comment_id="comment-1",
        text="Proxy svp",
        media_id="media-1",
        author_id="user-1",
        username="alice",
    )

    first = await processor.process_events([event])
    second = await processor.process_events([event])

    assert first == [ProcessingResult(comment_id="comment-1", status="processed")]
    assert second == [ProcessingResult(comment_id="comment-1", status="duplicate")]
    assert graph.calls == [
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy", None),
        ("public_reply", "comment-1", "C'est envoyé, check tes DM"),
    ]


@pytest.mark.asyncio
async def test_processor_likes_matching_facebook_comment(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="facebook", comment_id="comment-1", text="Proxy svp")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="processed")]
    assert graph.calls == [
        ("like", "comment-1"),
        ("facebook_can_reply_privately", "comment-1"),
        ("facebook_public_reply", "comment-1", "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger."),
        ("facebook_private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy"),
    ]


@pytest.mark.asyncio
async def test_processor_uses_short_graph_comment_id_for_facebook_actions(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="facebook", comment_id="post-1_comment-1", text="Proxy svp")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="post-1_comment-1", status="processed")]
    assert graph.calls == [
        ("like", "comment-1"),
        ("facebook_can_reply_privately", "post-1_comment-1"),
        ("facebook_public_reply", "comment-1", "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger."),
        ("facebook_private_reply", "page-1", "post-1_comment-1", "Voici la ressource demandée : https://example.com/proxy"),
    ]
    assert store.was_processed("facebook", "post-1_comment-1") is True


@pytest.mark.asyncio
async def test_processor_continues_facebook_flow_when_like_fails(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = LikeFailingGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="facebook", comment_id="comment-1", text="Proxy svp")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="processed")]
    assert graph.calls == [
        ("like", "comment-1"),
        ("facebook_can_reply_privately", "comment-1"),
        ("facebook_public_reply", "comment-1", "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger."),
        ("facebook_private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy"),
    ]



@pytest.mark.asyncio
async def test_processor_follow_gate_facebook_sends_prompt_not_link(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = ProcessedCommentStore(database)
    rule_store = CampaignRuleStore(database)
    follow_gate_store = FollowGateStore(database)
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="herdr",
            name="HERDR",
            platform="any",
            media_id="fb-media-1",
            keywords=["herdr"],
            public_reply_text="C'est envoyé ! Check tes messages privés !",
            dm_text="Voici le site : https://herdr.dev/",
            enabled=True,
            delivery_mode="follow_gate",
        )
    )
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        rule_store=rule_store,
        follow_gate_store=follow_gate_store,
        page_id="page-1",
    )
    event = CommentEvent(
        platform="facebook",
        comment_id="post-1_comment-1",
        text="HERDR",
        media_id="fb-media-1",
        author_id="user-1",
        username="alice",
    )

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="post-1_comment-1", status="follow_gate_pending")]
    assert graph.calls == [
        ("like", "comment-1"),
        ("facebook_can_reply_privately", "post-1_comment-1"),
        ("private_reply", "page-1", "post-1_comment-1", graph.calls[2][3], graph.calls[2][4]),
        ("facebook_public_reply", "comment-1", "Je t’ai envoyé les instructions en privé. Réponds « ✅ J’ai suivi » dans le message privé et je t’envoie le lien."),
    ]
    assert "https://herdr.dev/" not in graph.calls[2][3]
    gate = follow_gate_store.pending_for_sender("facebook", "user-1")
    assert gate is not None
    assert gate.dm_text == "Voici le site : https://herdr.dev/"
    assert store.delivery_state("facebook", "post-1_comment-1") == {
        "status": "follow_gate_pending",
        "like_sent": True,
        "public_reply_sent": True,
        "dm_sent": False,
    }

@pytest.mark.asyncio
async def test_processor_uses_default_public_reply_when_config_is_blank(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="",
    )
    event = CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="processed")]
    assert graph.calls == [
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy", None),
        ("public_reply", "comment-1", "C'est envoyé ! Check tes messages privés !"),
    ]


@pytest.mark.asyncio
async def test_processor_ignores_non_matching_keyword(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(
        platform="instagram",
        comment_id="comment-1",
        text="hello",
        media_id="media-1",
        author_id="user-1",
        username="alice",
    )

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="ignored")]
    assert graph.calls == []


@pytest.mark.asyncio
async def test_processor_ignores_owner_comment_events(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="graphify",
        resource_url="https://example.com/graphify",
        public_reply_text="C'est envoyé, check tes DM",
        owner_ids={"page-1"},
        owner_usernames={"FloDev - Développement Web et Tutoriels vidéo"},
    )
    event = CommentEvent(
        platform="facebook",
        comment_id="post-1_owner-comment",
        text="Graphify should not trigger on our own reply",
        author_id="page-1",
        username="FloDev - Développement Web et Tutoriels vidéo",
    )

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="post-1_owner-comment", status="ignored_owner")]
    assert graph.calls == []
    assert store.delivery_state("facebook", "post-1_owner-comment") is None


@pytest.mark.asyncio
async def test_processor_does_not_use_legacy_env_fallback_when_enabled_rules_exist_but_media_is_missing(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = ProcessedCommentStore(database)
    rule_store = CampaignRuleStore(database)
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="proxy-ep9",
            name="Proxy EP9",
            platform="any",
            media_id="instagram-media-ep9",
            keywords=["proxy"],
            public_reply_text="C'est envoyé check tes DMs !",
            dm_text="Lien EP9 correct",
            enabled=True,
        )
    )
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        rule_store=rule_store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/resource",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy", media_id=None)

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="ignored")]
    assert graph.calls == []
    assert store.delivery_state("instagram", "comment-1") is None


@pytest.mark.asyncio
async def test_processor_returns_failed_status_when_graph_api_fails_and_allows_retry(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    failing_graph = FailingGraphClient()
    processor = CommentProcessor(
        graph_client=failing_graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy")

    failed = await processor.process_events([event])

    assert failed == [ProcessingResult(comment_id="comment-1", status="failed_graph_api")]
    assert failing_graph.calls == [
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy", None),
        ("public_reply", "comment-1", "C'est envoyé, check tes DM"),
    ]

    working_graph = FakeGraphClient()
    retry_processor = CommentProcessor(
        graph_client=working_graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )

    retried = await retry_processor.process_events([event])

    assert retried == [ProcessingResult(comment_id="comment-1", status="processed")]
    assert working_graph.calls == [
        ("public_reply", "comment-1", "C'est envoyé, check tes DM"),
    ]


@pytest.mark.asyncio
async def test_processor_retries_missing_public_reply_without_duplicate_dm(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    store.mark_processed(
        platform="facebook",
        comment_id="post-1_comment-1",
        keyword="proxy",
        like_sent=False,
        public_reply_sent=False,
        dm_sent=True,
    )
    age_processed_rows(tmp_path / "processed.sqlite3")
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="facebook", comment_id="post-1_comment-1", text="Proxy")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="post-1_comment-1", status="processed")]
    assert graph.calls == [
        ("like", "comment-1"),
        ("facebook_public_reply", "comment-1", "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger."),
    ]
    assert store.delivery_state("facebook", "post-1_comment-1") == {
        "status": "processed",
        "like_sent": True,
        "public_reply_sent": True,
        "dm_sent": True,
    }


@pytest.mark.asyncio
async def test_processor_uses_personal_fallback_when_facebook_dm_is_not_allowed(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = FacebookPrivateReplyBlockedGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="idlepay",
        resource_url="https://www.idlepay.co/",
        public_reply_text="C'est envoyé check tes DMs !",
    )
    event = CommentEvent(platform="facebook", comment_id="post-1_comment-1", text="Idlepay")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="post-1_comment-1", status="private_reply_blocked")]
    assert graph.calls == [
        ("like", "comment-1"),
        ("facebook_can_reply_privately", "post-1_comment-1"),
        (
            "facebook_public_reply",
            "comment-1",
            "Meta ne me laisse pas t’écrire en premier 😅\nEnvoie-moi un petit message et je t’envoie le lien.",
        ),
    ]
    assert store.delivery_state("facebook", "post-1_comment-1") == {
        "status": "private_reply_blocked",
        "like_sent": True,
        "public_reply_sent": True,
        "dm_sent": False,
    }


@pytest.mark.asyncio
async def test_processor_uses_fallback_when_instagram_private_reply_fails_before_public_reply(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = InstagramPrivateReplyBlockedGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="private_reply_blocked")]
    assert graph.calls == [
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy", None),
        (
            "public_reply",
            "comment-1",
            "Meta ne me laisse pas t’écrire en premier 😅\nEnvoie-moi un petit message et je t’envoie le lien.",
        ),
    ]
    assert store.delivery_state("instagram", "comment-1") == {
        "status": "private_reply_blocked",
        "like_sent": False,
        "public_reply_sent": True,
        "dm_sent": False,
    }


@pytest.mark.asyncio
async def test_processor_records_outbound_public_reply_and_private_reply(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = ProcessedCommentStore(database)
    outbound_store = OutboundMessageStore(database)
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        outbound_store=outbound_store,
        page_id="page-1",
        keyword="proxy",
        resource_url="https://example.com/proxy",
        public_reply_text="C'est envoyé, check tes DM",
    )
    event = CommentEvent(platform="facebook", comment_id="post-1_comment-1", text="Proxy svp")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="post-1_comment-1", status="processed")]
    rows = outbound_store.list_for_source("facebook", "post-1_comment-1")
    assert [row["message_type"] for row in rows] == ["public_reply", "private_reply"]
    assert [row["message_text"] for row in rows] == [
        "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger.",
        "Voici la ressource demandée : https://example.com/proxy",
    ]
    assert [row["meta_response_id"] for row in rows] == ["reply-id", "message-id"]


@pytest.mark.asyncio
async def test_follow_gate_prompts_first_then_unlocks_on_instagram_confirmation(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = ProcessedCommentStore(database)
    outbound_store = OutboundMessageStore(database)
    gate_store = FollowGateStore(database)
    rule_store = CampaignRuleStore(database)
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="blog",
            name="Blog",
            platform="instagram",
            media_id="media-1",
            keywords=["blog"],
            public_reply_text="Je t’envoie les étapes en privé",
            dm_text="Voici le lien final",
            enabled=True,
            delivery_mode="follow_gate",
        )
    )
    graph = FakeGraphClient()
    processor = CommentProcessor(
        graph_client=graph,
        store=store,
        outbound_store=outbound_store,
        rule_store=rule_store,
        follow_gate_store=gate_store,
        page_id="page-1",
    )
    event = CommentEvent(
        platform="instagram",
        comment_id="comment-1",
        text="Blog",
        media_id="media-1",
        author_id="igsid-1",
        username="alice",
    )

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="instagram_follow_gate_disabled")]
    assert graph.calls == []
    assert gate_store.pending_for_sender("instagram", "igsid-1") is None
    assert store.delivery_state("instagram", "comment-1") == {
        "status": "private_reply_blocked",
        "like_sent": False,
        "public_reply_sent": False,
        "dm_sent": False,
    }


@pytest.mark.asyncio
async def test_follow_gate_confirmation_without_pending_gate_is_not_handled(tmp_path):
    database = tmp_path / "processed.sqlite3"
    graph = FakeGraphClient()
    processor = PrivateMessageProcessor(
        graph_client=graph,
        follow_gate_store=FollowGateStore(database),
        processed_store=ProcessedCommentStore(database),
    )

    result = await processor.process_events([PrivateMessageEvent("instagram_dm", "mid-1", "j'ai suivi", "igsid-1")])

    assert result[0].status == "no_pending_follow_gate"
    assert graph.calls == []


def _instagram_follow_gate_setup(tmp_path, *, instagram_messaging_enabled):
    database = tmp_path / "processed.sqlite3"
    rule_store = CampaignRuleStore(database)
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="blog",
            name="Blog",
            platform="instagram",
            media_id="media-1",
            keywords=["blog"],
            public_reply_text="Je t’envoie les étapes en privé",
            dm_text="Voici le lien final",
            enabled=True,
            delivery_mode="follow_gate",
        )
    )
    graph = FakeGraphClient()
    gate_store = FollowGateStore(database)
    processor = CommentProcessor(
        graph_client=graph,
        store=ProcessedCommentStore(database),
        outbound_store=OutboundMessageStore(database),
        rule_store=rule_store,
        follow_gate_store=gate_store,
        page_id="page-1",
        instagram_messaging_enabled=instagram_messaging_enabled,
    )
    event = CommentEvent(
        platform="instagram",
        comment_id="comment-1",
        text="Blog",
        media_id="media-1",
        author_id="igsid-1",
        username="alice",
    )
    return processor, graph, gate_store, event


@pytest.mark.asyncio
async def test_instagram_follow_gate_sends_prompt_once_instagram_secret_is_configured(tmp_path):
    processor, graph, gate_store, event = _instagram_follow_gate_setup(tmp_path, instagram_messaging_enabled=True)

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="follow_gate_pending")]
    assert graph.calls[0][0] == "private_reply"
    assert gate_store.pending_for_sender("instagram", "igsid-1") is not None


class InstagramRateLimitedGraphClient(FakeGraphClient):
    async def private_reply_to_instagram_comment(self, page_id: str, comment_id: str, text: str, quick_replies=None):
        self.calls.append(("private_reply", page_id, comment_id, text, quick_replies))
        raise GraphAPIError("Meta Graph API error 4: Application request limit reached", code=4)


@pytest.mark.asyncio
async def test_instagram_transient_private_reply_error_is_retryable_not_blocked(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    graph = InstagramRateLimitedGraphClient()
    processor = CommentProcessor(graph_client=graph, store=store, page_id="page-1", resource_url="https://example.com/proxy")
    event = CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="failed_graph_api")]
    # No "Meta ne me laisse pas t'écrire" fallback posted publicly for a rate limit.
    assert [call[0] for call in graph.calls] == ["private_reply"]
    assert store.comment_status("instagram", "comment-1") == "failed"
    assert store.try_claim("instagram", "comment-1", "proxy") is True


class NetworkDownGraphClient(FakeGraphClient):
    async def private_reply_to_instagram_comment(self, page_id: str, comment_id: str, text: str, quick_replies=None):
        raise ConnectionError("graph.facebook.com unreachable")


@pytest.mark.asyncio
async def test_unexpected_error_releases_claim_instead_of_leaving_it_stuck(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    processor = CommentProcessor(
        graph_client=NetworkDownGraphClient(), store=store, page_id="page-1", resource_url="https://example.com/proxy"
    )
    event = CommentEvent(platform="instagram", comment_id="comment-1", text="Proxy")

    result = await processor.process_events([event])

    assert result == [ProcessingResult(comment_id="comment-1", status="failed_unexpected")]
    assert store.comment_status("instagram", "comment-1") == "failed"
    assert store.try_claim("instagram", "comment-1", "proxy") is True


@pytest.mark.asyncio
async def test_meta_retry_during_in_flight_run_is_a_duplicate(tmp_path):
    store = ProcessedCommentStore(tmp_path / "processed.sqlite3")
    # First run checkpointed the DM and is still about to post the public reply.
    store.mark_processed(
        platform="facebook",
        comment_id="post-1_comment-1",
        keyword="proxy",
        like_sent=True,
        public_reply_sent=False,
        dm_sent=True,
    )
    graph = FakeGraphClient()
    processor = CommentProcessor(graph_client=graph, store=store, page_id="page-1", resource_url="https://example.com/proxy")

    result = await processor.process_events([CommentEvent(platform="facebook", comment_id="post-1_comment-1", text="Proxy")])

    assert result == [ProcessingResult(comment_id="post-1_comment-1", status="duplicate")]
    assert graph.calls == []
