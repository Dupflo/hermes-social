import pytest

from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.graph_client import GraphAPIError
from app.outbound_message_store import OutboundMessageStore
from app.processor import CommentProcessor, ProcessingResult
from app.store import ProcessedCommentStore
from app.webhook_parser import CommentEvent


class FakeGraphClient:
    def __init__(self):
        self.calls = []

    async def like_comment(self, comment_id: str):
        self.calls.append(("like", comment_id))
        return {"success": True}

    async def reply_to_instagram_comment(self, comment_id: str, message: str):
        self.calls.append(("public_reply", comment_id, message))
        return {"id": "reply-id"}

    async def private_reply_to_instagram_comment(self, page_id: str, comment_id: str, text: str):
        self.calls.append(("private_reply", page_id, comment_id, text))
        return {"message_id": "message-id"}

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
    async def private_reply_to_instagram_comment(self, page_id: str, comment_id: str, text: str):
        self.calls.append(("private_reply", page_id, comment_id, text))
        raise GraphAPIError("Meta Graph API error 10901: Activity replying time expired")


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
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy"),
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
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy"),
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
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy"),
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
        ("private_reply", "page-1", "comment-1", "Voici la ressource demandée : https://example.com/proxy"),
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
