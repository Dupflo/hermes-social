from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.webhook_manual_review import reconcile_manual_review_replies
from app.webhook_parser import parse_comment_events


def test_parse_facebook_owner_reply_exposes_parent_comment_id():
    payload = {
        "object": "page",
        "entry": [
            {
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "comment_id": "reply-1",
                            "parent_id": "post-1_comment-1",
                            "post_id": "post-1",
                            "message": "Merci pour ton retour",
                            "from": {"id": "page-1", "name": "FloDev"},
                        },
                    }
                ]
            }
        ],
    }

    events = parse_comment_events(payload)

    assert events[0].comment_id == "reply-1"
    assert events[0].parent_id == "post-1_comment-1"
    assert events[0].author_id == "page-1"


def test_reconcile_manual_review_replies_marks_parent_item_manually_replied(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    store.upsert_pending(
        CommentReviewItem(
            platform="facebook",
            comment_id="post-1_comment-1",
            media_id="post-1",
            username="alice",
            text="Le lien svp",
            media_permalink="https://facebook.test/reel/1",
            media_caption="caption",
            reason="direct_request",
            score=80,
        )
    )
    store.mark_in_review("facebook", "post-1_comment-1")
    event = parse_comment_events(
        {
            "object": "page",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "comment_id": "reply-1",
                                "parent_id": "post-1_comment-1",
                                "post_id": "post-1",
                                "message": "Réponse manuelle",
                                "from": {"id": "page-1", "name": "FloDev"},
                            },
                        }
                    ]
                }
            ],
        }
    )[0]

    count = reconcile_manual_review_replies(
        events=[event],
        review_store=store,
        owner_ids={"page-1"},
        owner_usernames={"dupflodev"},
    )

    assert count == 1
    item = store.get("facebook", "post-1_comment-1")
    assert item.status == "manually_replied"
    assert item.owner_reply_id == "reply-1"


def test_reconcile_manual_review_replies_marks_pending_thread_replies_under_same_parent(tmp_path):
    store = CommentReviewStore(tmp_path / "review.sqlite3")
    store.upsert_pending(
        CommentReviewItem(
            platform="facebook",
            comment_id="root-comment",
            media_id="post-1",
            username="alice",
            text="Repo ?",
            media_permalink="https://facebook.test/reel/1",
            media_caption="caption",
            reason="direct_request",
            score=80,
        )
    )
    store.mark_replied("facebook", "root-comment", posted_reply_id="old-owner-reply")
    store.upsert_pending(
        CommentReviewItem(
            platform="facebook",
            comment_id="user-followup",
            media_id="post-1",
            username="alice",
            text="Merci, et ensuite ?",
            media_permalink="https://facebook.test/reel/1",
            media_caption="caption",
            reason="thread_reply",
            score=95,
            parent_id="root-comment",
        )
    )
    event = parse_comment_events(
        {
            "object": "page",
            "entry": [
                {
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "comment_id": "new-owner-reply",
                                "parent_id": "root-comment",
                                "post_id": "post-1",
                                "message": "Réponse manuelle dans le fil",
                                "from": {"id": "page-1", "name": "FloDev"},
                            },
                        }
                    ]
                }
            ],
        }
    )[0]

    count = reconcile_manual_review_replies(
        events=[event],
        review_store=store,
        owner_ids={"page-1"},
        owner_usernames={"dupflodev"},
    )

    assert count == 1
    item = store.get("facebook", "user-followup")
    assert item is not None
    assert item.status == "manually_replied"
    assert item.owner_reply_id == "new-owner-reply"
