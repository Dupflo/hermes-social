import pytest

from app.backfill_comments import BackfillCandidate
from app.campaign_rules import CampaignRuleStore
from app.comment_review_store import CommentReviewStore
from app.scan_comment_reviews import RawMediaItem, run_scan_from_fetcher
from app.store import ProcessedCommentStore


class FakeFetcher:
    async def fetch_instagram_comments(self, ig_user_id, *, media_limit, comments_limit):
        return [
            BackfillCandidate(
                platform="instagram",
                comment_id="ig-comment-1",
                text="Le lien svp",
                media_id="ig-media-1",
                username="alice",
            )
        ]

    async def fetch_facebook_comments(self, page_id, *, post_limit, comments_limit):
        return [
            BackfillCandidate(
                platform="facebook",
                comment_id="fb-post-1_fb-comment-1",
                text="Merci !",
                media_id="fb-post-1",
                username="bob",
            )
        ]

    async def fetch_instagram_media(self, ig_user_id, *, media_limit):
        return [
            RawMediaItem(
                platform="instagram",
                media_id="ig-media-1",
                permalink="https://instagram.test/reel/1",
                caption="caption ig",
                comments_count=1,
            )
        ]

    async def fetch_facebook_posts(self, page_id, *, post_limit):
        return [
            RawMediaItem(
                platform="facebook",
                media_id="fb-post-1",
                permalink="https://facebook.test/reel/1",
                caption="caption fb",
            )
        ]


@pytest.mark.asyncio
async def test_run_scan_from_fetcher_populates_review_queue(tmp_path):
    db = tmp_path / "app.sqlite3"
    summary = await run_scan_from_fetcher(
        fetcher=FakeFetcher(),
        platform="all",
        page_id="page-1",
        ig_user_id="ig-user-1",
        media_limit=10,
        comments_limit=20,
        rule_store=CampaignRuleStore(db),
        processed_store=ProcessedCommentStore(db),
        review_store=CommentReviewStore(db),
    )

    assert summary.scanned_comments == 2
    assert summary.inserted_pending == 1
    item = CommentReviewStore(db).next_pending()
    assert item.platform == "instagram"
    assert item.comment_id == "ig-comment-1"
    assert item.media_permalink == "https://instagram.test/reel/1"


class IncompleteInstagramFetcher(FakeFetcher):
    async def fetch_instagram_media(self, ig_user_id, *, media_limit):
        return [
            RawMediaItem(
                platform="instagram",
                media_id="ig-media-1",
                permalink="https://instagram.test/reel/1",
                caption="caption ig",
                comments_count=64,
            )
        ]

    async def fetch_instagram_comments(self, ig_user_id, *, media_limit, comments_limit):
        return [
            BackfillCandidate(
                platform="instagram",
                comment_id="ig-comment-1",
                text="Proxy",
                media_id="ig-media-1",
                username="alice",
            ),
            BackfillCandidate(
                platform="instagram",
                comment_id="ig-comment-2",
                text="Question",
                media_id="ig-media-1",
                username="bob",
            ),
        ]


@pytest.mark.asyncio
async def test_run_scan_reports_incomplete_instagram_media_visibility(tmp_path):
    db = tmp_path / "app.sqlite3"
    summary = await run_scan_from_fetcher(
        fetcher=IncompleteInstagramFetcher(),
        platform="instagram",
        page_id="page-1",
        ig_user_id="ig-user-1",
        media_limit=10,
        comments_limit=20,
        rule_store=CampaignRuleStore(db),
        processed_store=ProcessedCommentStore(db),
        review_store=CommentReviewStore(db),
    )

    assert summary.incomplete_media == [
        {
            "platform": "instagram",
            "media_id": "ig-media-1",
            "permalink": "https://instagram.test/reel/1",
            "reported_comments_count": 64,
            "fetched_comments_count": 2,
        }
    ]
