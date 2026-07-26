import pytest

from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.outbound_message_store import OutboundMessageStore
from app.review_comments import format_link_context, format_next_item, post_review_reply, select_next_item, skip_review_item


class FakeGraphClient:
    def __init__(self):
        self.calls = []

    async def reply_to_facebook_comment(self, comment_id: str, message: str):
        self.calls.append(("facebook", comment_id, message))
        return {"id": "fb-reply-1"}

    async def reply_to_instagram_comment(self, comment_id: str, message: str):
        self.calls.append(("instagram", comment_id, message))
        return {"id": "ig-reply-1"}

    async def send_direct_message(self, recipient_id: str, text: str):
        self.calls.append(("dm", recipient_id, text))
        return {"message_id": "dm-message-1"}


def make_store(tmp_path):
    return CommentReviewStore(tmp_path / "review.sqlite3")


def add_item(store, *, platform="facebook", comment_id="post-1_comment-1", status="pending"):
    item = CommentReviewItem(
        platform=platform,
        comment_id=comment_id,
        media_id="post-1",
        username="alice",
        text="Le nom du site svp",
        media_permalink="https://facebook.test/reel/1",
        media_caption="Une vidéo intéressante",
        reason="direct_request",
        score=80,
        comment_permalink=None,
    )
    store.upsert_pending(item)
    if status == "in_review":
        store.mark_in_review(platform, comment_id)
    return item


def test_select_next_item_reuses_active_in_review_item_and_formats_for_telegram(tmp_path):
    store = make_store(tmp_path)
    item = add_item(store)

    selected = select_next_item(store)
    selected_again = select_next_item(store)

    assert selected.comment_id == item.comment_id
    assert selected_again.comment_id == item.comment_id
    assert selected_again.status == "in_review"
    message = format_next_item(selected_again, pending_total=1)
    assert "1 commentaire intéressant" in message
    assert "Plateforme: Facebook" in message
    assert "Le nom du site svp" in message
    assert "dis `lien`" in message


def test_format_link_context_returns_practical_platform_context(tmp_path):
    store = make_store(tmp_path)
    add_item(store)

    message = format_link_context(store.link_context("facebook", "post-1_comment-1"))

    assert "https://facebook.test/reel/1" in message
    assert "post-1_comment-1" in message
    assert "alice" in message


@pytest.mark.asyncio
async def test_post_review_reply_posts_to_facebook_short_comment_id_and_marks_replied(tmp_path):
    store = make_store(tmp_path)
    add_item(store, status="in_review")
    graph = FakeGraphClient()

    result = await post_review_reply(store, graph, platform="facebook", comment_id="post-1_comment-1", text="Voici le site 👍")

    assert graph.calls == [("facebook", "comment-1", "Voici le site 👍")]
    assert result == "Réponse postée ✅"
    item = store.get("facebook", "post-1_comment-1")
    assert item.status == "replied"
    assert item.posted_reply_id == "fb-reply-1"


@pytest.mark.asyncio
async def test_post_review_reply_refuses_manually_replied_item(tmp_path):
    store = make_store(tmp_path)
    add_item(store, status="in_review")
    store.mark_manually_replied("facebook", "post-1_comment-1", owner_reply_id="manual")
    graph = FakeGraphClient()

    result = await post_review_reply(store, graph, platform="facebook", comment_id="post-1_comment-1", text="Trop tard")

    assert result == "Déjà répondu manuellement sur la plateforme."
    assert graph.calls == []


def test_skip_review_item_marks_item_skipped(tmp_path):
    store = make_store(tmp_path)
    add_item(store, status="in_review")

    assert skip_review_item(store, "facebook", "post-1_comment-1") == "Commentaire ignoré ✅"
    assert store.get("facebook", "post-1_comment-1").status == "skipped"


@pytest.mark.asyncio
async def test_post_review_reply_sends_facebook_dm_to_psid_and_marks_replied(tmp_path):
    store = make_store(tmp_path)
    add_item(store, platform="facebook_dm", comment_id="mid-1", status="in_review")
    graph = FakeGraphClient()

    result = await post_review_reply(store, graph, platform="facebook_dm", comment_id="mid-1", text="Réponse ferme")

    assert graph.calls == [("dm", "alice", "Réponse ferme")]
    assert result == "Message privé envoyé ✅"
    item = store.get("facebook_dm", "mid-1")
    assert item.status == "replied"
    assert item.posted_reply_id == "dm-message-1"


@pytest.mark.asyncio
async def test_post_review_reply_records_outbound_message(tmp_path):
    database = tmp_path / "review.sqlite3"
    store = CommentReviewStore(database)
    outbound_store = OutboundMessageStore(database)
    add_item(store, status="in_review")
    graph = FakeGraphClient()

    result = await post_review_reply(
        store,
        graph,
        platform="facebook",
        comment_id="post-1_comment-1",
        text="Voici le site 👍",
        outbound_store=outbound_store,
    )

    assert result == "Réponse postée ✅"
    rows = outbound_store.list_for_source("facebook", "post-1_comment-1")
    assert len(rows) == 1
    assert rows[0]["source_type"] == "manual_review"
    assert rows[0]["recipient_id"] == "comment-1"
    assert rows[0]["message_type"] == "public_reply"
    assert rows[0]["message_text"] == "Voici le site 👍"
    assert rows[0]["meta_response_id"] == "fb-reply-1"
