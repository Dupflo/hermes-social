import argparse
import asyncio
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.backfill_comments import BackfillCandidate, MetaCommentFetcher, dedupe_comments
from app.campaign_rules import CampaignRuleStore
from app.comment_review_scanner import MediaContext, ScanReviewSummary, scan_review_candidates
from app.comment_review_store import CommentReviewStore
from app.config import get_settings
from app.platform_utils import owner_identity_sets
from app.store import ProcessedCommentStore


@dataclass(frozen=True)
class RawMediaItem:
    platform: str
    media_id: str
    permalink: str | None
    caption: str | None
    comments_count: int | None = None


class ReviewScanFetcher(Protocol):
    async def fetch_instagram_comments(self, ig_user_id: str, *, media_limit: int, comments_limit: int) -> list[BackfillCandidate]: ...

    async def fetch_facebook_comments(self, page_id: str, *, post_limit: int, comments_limit: int) -> list[BackfillCandidate]: ...

    async def fetch_instagram_media(self, ig_user_id: str, *, media_limit: int) -> list[RawMediaItem]: ...

    async def fetch_facebook_posts(self, page_id: str, *, post_limit: int) -> list[RawMediaItem]: ...


class MetaReviewScanFetcher(MetaCommentFetcher):
    async def fetch_instagram_media(self, ig_user_id: str, *, media_limit: int) -> list[RawMediaItem]:
        items = await self._get_all(
            f"/{ig_user_id}/media",
            params={"fields": "id,caption,timestamp,permalink,comments_count", "limit": str(media_limit)},
            max_items=media_limit,
        )
        return [
            RawMediaItem(
                platform="instagram",
                media_id=item["id"],
                permalink=item.get("permalink"),
                caption=item.get("caption"),
                comments_count=item.get("comments_count"),
            )
            for item in items
            if item.get("id")
        ]

    async def fetch_facebook_posts(self, page_id: str, *, post_limit: int) -> list[RawMediaItem]:
        items = await self._get_all(
            f"/{page_id}/posts",
            params={"fields": "id,message,created_time,permalink_url", "limit": str(post_limit)},
            max_items=post_limit,
        )
        return [
            RawMediaItem(
                platform="facebook",
                media_id=item["id"],
                permalink=item.get("permalink_url"),
                caption=item.get("message"),
            )
            for item in items
            if item.get("id")
        ]


async def run_scan_from_fetcher(
    *,
    fetcher: ReviewScanFetcher,
    platform: str,
    page_id: str,
    ig_user_id: str,
    media_limit: int,
    comments_limit: int,
    rule_store: CampaignRuleStore,
    processed_store: ProcessedCommentStore,
    review_store: CommentReviewStore,
    interest_only_keywords: set[str] | None = None,
) -> ScanReviewSummary:
    comments: list[BackfillCandidate] = []
    media: list[RawMediaItem] = []
    if platform in {"instagram", "all"}:
        media.extend(await fetcher.fetch_instagram_media(ig_user_id, media_limit=media_limit))
        comments.extend(
            await fetcher.fetch_instagram_comments(ig_user_id, media_limit=media_limit, comments_limit=comments_limit)
        )
    if platform in {"facebook", "all"}:
        media.extend(await fetcher.fetch_facebook_posts(page_id, post_limit=media_limit))
        comments.extend(await fetcher.fetch_facebook_comments(page_id, post_limit=media_limit, comments_limit=comments_limit))
    media_contexts = {
        (item.platform, item.media_id): MediaContext(permalink=item.permalink, caption=item.caption) for item in media
    }
    summary = scan_review_candidates(
        comments=dedupe_comments(comments),
        media_contexts=media_contexts,
        rule_store=rule_store,
        processed_store=processed_store,
        review_store=review_store,
        interest_only_keywords=interest_only_keywords,
    )
    return ScanReviewSummary(
        scanned_comments=summary.scanned_comments,
        inserted_pending=summary.inserted_pending,
        manually_replied=summary.manually_replied,
        ignored=summary.ignored,
        pending_total=summary.pending_total,
        incomplete_media=_detect_incomplete_media(media, comments),
    )


def _detect_incomplete_media(
    media: list[RawMediaItem], comments: list[BackfillCandidate]
) -> list[dict[str, str | int | None]]:
    fetched_by_media: dict[tuple[str, str], int] = {}
    for comment in dedupe_comments(comments):
        if not comment.media_id:
            continue
        key = (comment.platform, comment.media_id)
        fetched_by_media[key] = fetched_by_media.get(key, 0) + 1
    warnings: list[dict[str, str | int | None]] = []
    for item in media:
        if item.platform != "instagram" or item.comments_count is None:
            continue
        fetched = fetched_by_media.get((item.platform, item.media_id), 0)
        if item.comments_count >= 20 and fetched < max(10, item.comments_count // 2):
            warnings.append(
                {
                    "platform": item.platform,
                    "media_id": item.media_id,
                    "permalink": item.permalink,
                    "reported_comments_count": item.comments_count,
                    "fetched_comments_count": fetched,
                }
            )
    return warnings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Meta comments into the manual review queue")
    parser.add_argument("--platform", choices=["facebook", "instagram", "all"], default="all")
    parser.add_argument("--media-limit", type=int, default=200)
    parser.add_argument("--comments-limit", type=int, default=1000)
    parser.add_argument("--db", default="data/processed_comments.sqlite3")
    return parser


async def async_main(argv: list[str] | None = None) -> str:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    rule_store = CampaignRuleStore(args.db)
    processed_store = ProcessedCommentStore(args.db)
    review_store = CommentReviewStore(args.db)
    owner_ids, owner_usernames = owner_identity_sets(settings)
    async with httpx.AsyncClient(timeout=30) as http_client:
        fetcher = MetaReviewScanFetcher(
            access_token=settings.meta_page_access_token,
            http_client=http_client,
            api_version=settings.graph_api_version,
            owner_usernames=owner_usernames,
            owner_ids=owner_ids,
        )
        summary = await run_scan_from_fetcher(
            fetcher=fetcher,
            platform=args.platform,
            page_id=settings.meta_page_id,
            ig_user_id=settings.meta_ig_user_id,
            media_limit=args.media_limit,
            comments_limit=args.comments_limit,
            rule_store=rule_store,
            processed_store=processed_store,
            review_store=review_store,
            interest_only_keywords=_csv_set(settings.interest_only_keywords),
        )
    incomplete_lines = ""
    if summary.incomplete_media:
        incomplete_lines = "\n" + "\n".join(
            "incomplete_media="
            f"{item['platform']} media_id={item['media_id']} "
            f"reported={item['reported_comments_count']} fetched={item['fetched_comments_count']} "
            f"permalink={item['permalink']}"
            for item in summary.incomplete_media
        )
    return (
        f"scanned_comments={summary.scanned_comments}\n"
        f"inserted_pending={summary.inserted_pending}\n"
        f"manually_replied={summary.manually_replied}\n"
        f"ignored={summary.ignored}\n"
        f"pending_total={summary.pending_total}"
        f"{incomplete_lines}"
    )


def main(argv: list[str] | None = None) -> None:
    print(asyncio.run(async_main(argv)))


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


if __name__ == "__main__":
    main()
