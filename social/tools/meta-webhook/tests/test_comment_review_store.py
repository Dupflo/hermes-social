from app.comment_review_store import CommentReviewItem, CommentReviewStore


def test_review_store_inserts_and_returns_highest_priority_pending_item(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    low = CommentReviewItem(
        platform="facebook",
        comment_id="comment-low",
        media_id="media-1",
        username="alice",
        text="Repo",
        media_permalink="https://facebook.test/reel/1",
        media_caption="caption",
        reason="possible_unconfigured_keyword",
        score=40,
    )
    high = CommentReviewItem(
        platform="instagram",
        comment_id="comment-high",
        media_id="media-2",
        username="bob",
        text="Le nom du site svp",
        media_permalink="https://instagram.test/reel/2",
        media_caption="caption 2",
        reason="direct_request",
        score=80,
    )

    assert store.upsert_pending(low) is True
    assert store.upsert_pending(high) is True

    assert store.counts_by_status() == {"pending": 2}
    assert store.next_pending() == high


def test_review_store_does_not_reset_replied_item_to_pending(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    item = CommentReviewItem(
        platform="facebook",
        comment_id="comment-1",
        media_id="media-1",
        username="alice",
        text="Le nom du site svp",
        media_permalink="https://facebook.test/reel/1",
        media_caption="caption",
        reason="direct_request",
        score=80,
    )

    store.upsert_pending(item)
    store.mark_replied("facebook", "comment-1", posted_reply_id="reply-1")
    assert store.upsert_pending(item) is False

    assert store.get("facebook", "comment-1").status == "replied"
    assert store.next_pending() is None


def test_review_store_reports_false_when_upsert_updates_existing_pending_item(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    item = CommentReviewItem(
        platform="facebook",
        comment_id="comment-1",
        media_id="media-1",
        username="alice",
        text="Repo",
        media_permalink="https://facebook.test/reel/1",
        media_caption="caption",
        reason="possible_unconfigured_keyword",
        score=40,
    )
    updated = CommentReviewItem(
        platform="facebook",
        comment_id="comment-1",
        media_id="media-1",
        username="alice",
        text="Repo svp",
        media_permalink="https://facebook.test/reel/1",
        media_caption="caption",
        reason="direct_request",
        score=80,
    )

    assert store.upsert_pending(item) is True
    assert store.upsert_pending(updated) is False

    stored = store.get("facebook", "comment-1")
    assert stored is not None
    assert stored.text == "Repo svp"
    assert stored.reason == "direct_request"
    assert store.counts_by_status() == {"pending": 1}


def test_review_store_tracks_in_review_manual_reply_and_link_context(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    item = CommentReviewItem(
        platform="facebook",
        comment_id="post-1_comment-1",
        media_id="post-1",
        username="alice",
        text="Je n'ai rien reçu",
        media_permalink="https://facebook.test/reel/1",
        media_caption="caption",
        reason="automation_complaint",
        score=100,
        comment_permalink="https://facebook.test/comment/post-1_comment-1",
    )

    store.upsert_pending(item)
    store.mark_in_review("facebook", "post-1_comment-1")
    assert store.active_in_review() == item.__class__(**{**item.__dict__, "status": "in_review"})

    store.mark_manually_replied(
        "facebook",
        "post-1_comment-1",
        owner_reply_id="owner-reply-1",
        owner_replied_at="2026-07-24T20:00:00+00:00",
    )

    stored = store.get("facebook", "post-1_comment-1")
    assert stored.status == "manually_replied"
    assert stored.owner_reply_id == "owner-reply-1"
    assert stored.owner_replied_at == "2026-07-24T20:00:00+00:00"
    assert store.next_pending() is None
    assert store.link_context("facebook", "post-1_comment-1") == {
        "platform": "facebook",
        "comment_id": "post-1_comment-1",
        "username": "alice",
        "media_permalink": "https://facebook.test/reel/1",
        "comment_permalink": "https://facebook.test/comment/post-1_comment-1",
    }


def test_review_store_skip_and_error_remove_item_from_pending_flow(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    first = CommentReviewItem(
        platform="facebook",
        comment_id="comment-1",
        media_id=None,
        username=None,
        text="Repo",
        media_permalink=None,
        media_caption=None,
        reason="possible_unconfigured_keyword",
        score=40,
    )
    second = CommentReviewItem(
        platform="facebook",
        comment_id="comment-2",
        media_id=None,
        username=None,
        text="Rpi",
        media_permalink=None,
        media_caption=None,
        reason="possible_unconfigured_keyword",
        score=30,
    )

    store.upsert_pending(first)
    store.upsert_pending(second)
    store.mark_skipped("facebook", "comment-1")
    store.mark_error("facebook", "comment-2", "Meta error")

    assert store.next_pending() is None
    assert store.counts_by_status() == {"error": 1, "skipped": 1}


def test_review_store_marks_pending_thread_replies_by_parent_id(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    store.upsert_pending(
        CommentReviewItem(
            platform="instagram",
            comment_id="reply-1",
            media_id="media-1",
            username="alice",
            text="Merci, et ensuite ?",
            media_permalink="https://instagram.test/reel/1",
            media_caption="caption",
            reason="thread_reply",
            score=95,
            parent_id="root-1",
        )
    )

    count = store.mark_thread_replies_manually_replied(
        "instagram",
        "root-1",
        owner_reply_id="owner-reply-1",
        owner_replied_at="2026-07-25T10:00:00+0000",
    )

    assert count == 1
    item = store.get("instagram", "reply-1")
    assert item is not None
    assert item.status == "manually_replied"
    assert item.owner_reply_id == "owner-reply-1"
    assert item.owner_replied_at == "2026-07-25T10:00:00+0000"
    assert item.parent_id == "root-1"
