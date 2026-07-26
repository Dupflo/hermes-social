from app.backfill_comments import BackfillCandidate
from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.comment_review_scanner import MediaContext, scan_review_candidates
from app.comment_review_store import CommentReviewStore
from app.store import ProcessedCommentStore


def test_scan_review_candidates_stores_only_interesting_unhandled_comments(tmp_path):
    db = tmp_path / "app.sqlite3"
    rule_store = CampaignRuleStore(db)
    processed_store = ProcessedCommentStore(db)
    review_store = CommentReviewStore(db)
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="proxy",
            name="Proxy",
            platform="any",
            media_id="media-proxy",
            keywords=["proxy"],
            public_reply_text="ok",
            dm_text="dm",
            enabled=True,
        )
    )
    processed_store.mark_processed(
        platform="facebook",
        comment_id="done",
        keyword="manual",
        like_sent=True,
        public_reply_sent=True,
        dm_sent=True,
    )

    summary = scan_review_candidates(
        comments=[
            BackfillCandidate(platform="facebook", comment_id="request", text="Le nom du site svp", media_id="media-1", username="alice"),
            BackfillCandidate(platform="facebook", comment_id="noise", text="Merci !", media_id="media-1", username="bob"),
            BackfillCandidate(platform="facebook", comment_id="campaign", text="Proxy", media_id="media-proxy", username="carol"),
            BackfillCandidate(platform="facebook", comment_id="done", text="Le lien svp", media_id="media-1", username="dan"),
        ],
        media_contexts={
            ("facebook", "media-1"): MediaContext(
                permalink="https://facebook.test/reel/1",
                caption="caption one",
            )
        },
        rule_store=rule_store,
        processed_store=processed_store,
        review_store=review_store,
    )

    assert summary.scanned_comments == 4
    assert summary.inserted_pending == 1
    assert summary.ignored == 3
    item = review_store.next_pending()
    assert item.comment_id == "request"
    assert item.reason == "direct_request"
    assert item.media_permalink == "https://facebook.test/reel/1"
    assert item.media_caption == "caption one"


def test_scan_review_candidates_marks_existing_item_manually_replied_when_owner_reply_appears(tmp_path):
    db = tmp_path / "app.sqlite3"
    rule_store = CampaignRuleStore(db)
    processed_store = ProcessedCommentStore(db)
    review_store = CommentReviewStore(db)
    review_store.upsert_pending(
        __import__("app.comment_review_store", fromlist=["CommentReviewItem"]).CommentReviewItem(
            platform="instagram",
            comment_id="comment-1",
            media_id="media-1",
            username="alice",
            text="Le lien svp",
            media_permalink="https://instagram.test/reel/1",
            media_caption="caption",
            reason="direct_request",
            score=80,
        )
    )
    review_store.mark_in_review("instagram", "comment-1")

    summary = scan_review_candidates(
        comments=[
            BackfillCandidate(
                platform="instagram",
                comment_id="comment-1",
                text="Le lien svp",
                media_id="media-1",
                username="alice",
                has_owner_reply=True,
                owner_reply_id="owner-reply-1",
                owner_reply_at="2026-07-24T20:00:00+0000",
            )
        ],
        media_contexts={},
        rule_store=rule_store,
        processed_store=processed_store,
        review_store=review_store,
    )

    assert summary.manually_replied == 1
    assert review_store.get("instagram", "comment-1").status == "manually_replied"
    assert review_store.active_in_review() is None


def test_scan_review_candidates_ignores_new_thread_reply_already_followed_by_owner_reply(tmp_path):
    db = tmp_path / "app.sqlite3"
    rule_store = CampaignRuleStore(db)
    processed_store = ProcessedCommentStore(db)
    review_store = CommentReviewStore(db)

    summary = scan_review_candidates(
        comments=[
            BackfillCandidate(
                platform="instagram",
                comment_id="reply-1",
                text="Merci, et ensuite ?",
                media_id="media-1",
                username="alice",
                parent_id="root-1",
                has_owner_reply=True,
                owner_reply_id="owner-reply-1",
            )
        ],
        media_contexts={},
        rule_store=rule_store,
        processed_store=processed_store,
        review_store=review_store,
    )

    assert summary.inserted_pending == 0
    assert summary.ignored == 1
    assert review_store.get("instagram", "reply-1") is None


def test_scan_review_candidates_ignores_interest_only_keyword(tmp_path):
    db = tmp_path / "app.sqlite3"
    rule_store = CampaignRuleStore(db)
    processed_store = ProcessedCommentStore(db)
    review_store = CommentReviewStore(db)

    summary = scan_review_candidates(
        comments=[BackfillCandidate(platform="instagram", comment_id="migration-1", text="Migration", media_id="media-1", username="tycalo9")],
        media_contexts={
            ("instagram", "media-1"): MediaContext(
                permalink="https://instagram.test/reel/1",
                caption="Tu veux le tuto inverse ? Commente MIGRATION",
            )
        },
        rule_store=rule_store,
        processed_store=processed_store,
        review_store=review_store,
        interest_only_keywords={"migration"},
    )

    assert summary.inserted_pending == 0
    assert summary.ignored == 1
    assert review_store.get("instagram", "migration-1") is None
