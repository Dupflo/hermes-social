import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.campaign_rules import CampaignRuleStore
from app.config import get_settings
from app.graph_client import GraphClient, GraphAPIError
from app.outbound_message_store import OutboundMessageStore
from app.platform_utils import owner_identity_sets
from app.processor import CommentProcessor
from app.store import ProcessedCommentStore
from app.webhook_parser import CommentEvent


FACEBOOK_THREAD_REPLIES_LIMIT = 100


@dataclass(frozen=True)
class BackfillCandidate:
    platform: str
    comment_id: str
    text: str
    media_id: str | None = None
    username: str | None = None
    has_owner_reply: bool = False
    owner_reply_id: str | None = None
    owner_reply_at: str | None = None
    parent_id: str | None = None


class MetaCommentFetcher:
    def __init__(
        self,
        access_token: str,
        http_client: httpx.AsyncClient,
        api_version: str = "v21.0",
        owner_usernames: set[str] | None = None,
        owner_ids: set[str] | None = None,
        page_size: int = 25,
    ) -> None:
        self.access_token = access_token
        self.http_client = http_client
        self.base_url = f"https://graph.facebook.com/{api_version}"
        self.owner_usernames = {username.lower() for username in (owner_usernames or set())}
        self.owner_ids = set(owner_ids or set())
        self.page_size = page_size

    async def fetch_instagram_comments(
        self,
        ig_user_id: str,
        *,
        media_limit: int = 25,
        comments_limit: int = 100,
    ) -> list[BackfillCandidate]:
        media_items = await self._get_all(
            f"/{ig_user_id}/media",
            params={"fields": "id,caption,timestamp,permalink", "limit": str(media_limit)},
            max_items=media_limit,
        )
        comments: list[BackfillCandidate] = []
        for media in media_items:
            media_id = media.get("id")
            if not media_id:
                continue
            for comment in await self._get_all(
                f"/{media_id}/comments",
                params={
                    "fields": "id,text,username,timestamp,replies{id,text,username,timestamp}",
                    "limit": str(comments_limit),
                },
                max_items=comments_limit,
            ):
                comment_id = comment.get("id")
                text = comment.get("text")
                if not comment_id or text is None:
                    continue
                comments.append(
                    BackfillCandidate(
                        platform="instagram",
                        comment_id=comment_id,
                        text=text,
                        media_id=media_id,
                        username=comment.get("username"),
                        has_owner_reply=self._has_owner_reply(comment),
                        owner_reply_id=self._owner_reply_field(comment, "id"),
                        owner_reply_at=self._owner_reply_field(comment, "timestamp")
                        or self._owner_reply_field(comment, "created_time"),
                    )
                )
                comments.extend(self._instagram_thread_replies(comment, media_id=media_id, parent_id=comment_id))
        return comments

    async def fetch_facebook_comments(
        self,
        page_id: str,
        *,
        post_limit: int = 25,
        comments_limit: int = 100,
    ) -> list[BackfillCandidate]:
        posts = await self._get_all(
            f"/{page_id}/posts",
            params={"fields": "id,message,created_time,permalink_url", "limit": str(post_limit)},
            max_items=post_limit,
        )
        comments: list[BackfillCandidate] = []
        for post in posts:
            post_id = post.get("id")
            if not post_id:
                continue
            for comment in await self._get_all(
                f"/{post_id}/comments",
                params={
                    "fields": f"id,message,from,created_time,comments.limit({FACEBOOK_THREAD_REPLIES_LIMIT}){{id,message,from,created_time}}",
                    "limit": str(comments_limit),
                },
                max_items=comments_limit,
            ):
                comment_id = comment.get("id")
                text = comment.get("message")
                if not comment_id or text is None:
                    continue
                if self._is_owner_reply(comment):
                    continue
                author = comment.get("from") or {}
                comments.append(
                    BackfillCandidate(
                        platform="facebook",
                        comment_id=comment_id,
                        text=text,
                        media_id=post_id,
                        username=author.get("name") or author.get("username"),
                        has_owner_reply=self._has_owner_reply(comment),
                        owner_reply_id=self._owner_reply_field(comment, "id"),
                        owner_reply_at=self._owner_reply_field(comment, "created_time")
                        or self._owner_reply_field(comment, "timestamp"),
                    )
                )
                comments.extend(self._facebook_thread_replies(comment, post_id=post_id, parent_id=comment_id))
        return comments

    def _instagram_thread_replies(
        self,
        comment: dict[str, Any],
        *,
        media_id: str,
        parent_id: str,
    ) -> list[BackfillCandidate]:
        replies = comment.get("replies") or {}
        if not isinstance(replies, dict):
            return []
        candidates: list[BackfillCandidate] = []
        reply_items = replies.get("data", [])
        for index, reply in enumerate(reply_items):
            if self._is_owner_reply(reply):
                continue
            reply_id = reply.get("id")
            text = reply.get("text")
            if not reply_id or text is None:
                continue
            owner_reply = self._next_owner_reply(reply_items, index)
            candidates.append(
                BackfillCandidate(
                    platform="instagram",
                    comment_id=reply_id,
                    text=text,
                    media_id=media_id,
                    username=reply.get("username"),
                    parent_id=parent_id,
                    has_owner_reply=owner_reply is not None,
                    owner_reply_id=str(owner_reply.get("id")) if owner_reply and owner_reply.get("id") else None,
                    owner_reply_at=self._reply_time(owner_reply),
                )
            )
        return candidates

    def _facebook_thread_replies(
        self,
        comment: dict[str, Any],
        *,
        post_id: str,
        parent_id: str,
    ) -> list[BackfillCandidate]:
        replies = comment.get("comments") or {}
        if not isinstance(replies, dict):
            return []
        candidates: list[BackfillCandidate] = []
        reply_items = replies.get("data", [])
        for index, reply in enumerate(reply_items):
            if self._is_owner_reply(reply):
                continue
            reply_id = reply.get("id")
            text = reply.get("message")
            if not reply_id or text is None:
                continue
            author = reply.get("from") or {}
            owner_reply = self._next_owner_reply(reply_items, index)
            candidates.append(
                BackfillCandidate(
                    platform="facebook",
                    comment_id=reply_id,
                    text=text,
                    media_id=post_id,
                    username=author.get("name") or author.get("username"),
                    parent_id=parent_id,
                    has_owner_reply=owner_reply is not None,
                    owner_reply_id=str(owner_reply.get("id")) if owner_reply and owner_reply.get("id") else None,
                    owner_reply_at=self._reply_time(owner_reply),
                )
            )
        return candidates

    def _next_owner_reply(self, replies: list[dict[str, Any]], index: int) -> dict[str, Any] | None:
        for reply in replies[index + 1 :]:
            if self._is_owner_reply(reply):
                return reply
        return None

    def _reply_time(self, reply: dict[str, Any] | None) -> str | None:
        if not reply:
            return None
        value = reply.get("timestamp") or reply.get("created_time")
        return str(value) if value is not None else None

    def _has_owner_reply(self, comment: dict[str, Any]) -> bool:
        return self._owner_reply(comment) is not None

    def _owner_reply_field(self, comment: dict[str, Any], field: str) -> str | None:
        reply = self._owner_reply(comment)
        value = reply.get(field) if reply else None
        return str(value) if value is not None else None

    def _owner_reply(self, comment: dict[str, Any]) -> dict[str, Any] | None:
        replies = comment.get("replies") or comment.get("comments") or {}
        if not isinstance(replies, dict):
            return None
        for reply in replies.get("data", []):
            if self._is_owner_reply(reply):
                return reply
        return None

    def _is_owner_reply(self, reply: dict[str, Any]) -> bool:
        author = reply.get("from") or {}
        author_id = author.get("id")
        if author_id and author_id in self.owner_ids:
            return True
        username = (reply.get("username") or author.get("username") or author.get("name") or "").lower()
        if username and username in self.owner_usernames:
            return True
        return False

    async def _get_all(self, path: str, *, params: dict[str, str], max_items: int) -> list[dict[str, Any]]:
        url = f"{self.base_url}{path}"
        request_params = {"access_token": self.access_token, **params}
        request_params["limit"] = str(min(int(request_params.get("limit", self.page_size)), self.page_size))
        items: list[dict[str, Any]] = []
        while url and len(items) < max_items:
            response = await self.http_client.get(url, params=request_params if "?" not in url else None)
            data = response.json() if response.content else {}
            if response.is_error:
                error = data.get("error", {}) if isinstance(data, dict) else {}
                raise GraphAPIError(f"Meta Graph API error {error.get('code')}: {error.get('message') or response.text}")
            items.extend(data.get("data", []))
            url = data.get("paging", {}).get("next")
            request_params = {}
        return items[:max_items]


def find_backfill_candidates(
    *,
    comments: list[BackfillCandidate],
    rule_store: CampaignRuleStore,
    processed_store: ProcessedCommentStore,
) -> list[BackfillCandidate]:
    candidates = []
    for comment in comments:
        event = CommentEvent(
            platform=comment.platform,
            comment_id=comment.comment_id,
            text=comment.text,
            media_id=comment.media_id,
            username=comment.username,
            parent_id=comment.parent_id,
        )
        if rule_store.find_matching_rule(event) is None:
            continue
        delivery_state = processed_store.delivery_state(comment.platform, comment.comment_id)
        if processed_store.is_terminal(comment.platform, comment.comment_id):
            continue
        if processed_store.is_fully_processed(comment.platform, comment.comment_id):
            continue
        if comment.has_owner_reply and not (delivery_state and not delivery_state["dm_sent"]):
            continue
        candidates.append(comment)
    return candidates


def filter_by_comment_ids(comments: list[BackfillCandidate], comment_ids: set[str]) -> list[BackfillCandidate]:
    if not comment_ids:
        return comments
    return [comment for comment in comments if comment.comment_id in comment_ids]


def dedupe_comments(comments: list[BackfillCandidate]) -> list[BackfillCandidate]:
    seen: set[tuple[str, str]] = set()
    deduped: list[BackfillCandidate] = []
    for comment in comments:
        key = (comment.platform, comment.comment_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(comment)
    return deduped


async def run_backfill(
    *,
    apply: bool,
    platform: str,
    media_limit: int,
    comments_limit: int,
    database_path: str,
    comment_ids: set[str] | None = None,
) -> list[dict[str, str | None]]:
    settings = get_settings()
    processed_store = ProcessedCommentStore(database_path)
    rule_store = CampaignRuleStore(database_path)
    owner_ids, owner_usernames = owner_identity_sets(settings)

    async with httpx.AsyncClient(timeout=30) as http_client:
        fetcher = MetaCommentFetcher(
            access_token=settings.meta_page_access_token,
            http_client=http_client,
            api_version=settings.graph_api_version,
            owner_usernames=owner_usernames,
            owner_ids=owner_ids,
        )
        comments: list[BackfillCandidate] = []
        if platform in {"instagram", "all"}:
            comments.extend(
                await fetcher.fetch_instagram_comments(
                    settings.meta_ig_user_id,
                    media_limit=media_limit,
                    comments_limit=comments_limit,
                )
            )
        if platform in {"facebook", "all"}:
            comments.extend(
                await fetcher.fetch_facebook_comments(
                    settings.meta_page_id,
                    post_limit=media_limit,
                    comments_limit=comments_limit,
                )
            )
        comments = dedupe_comments(comments)

        candidates = find_backfill_candidates(
            comments=comments,
            rule_store=rule_store,
            processed_store=processed_store,
        )
        candidates = filter_by_comment_ids(candidates, comment_ids or set())
        if not apply:
            return [
                {
                    "platform": candidate.platform,
                    "comment_id": candidate.comment_id,
                    "media_id": candidate.media_id,
                    "username": candidate.username,
                    "text": candidate.text,
                    "status": "candidate",
                }
                for candidate in candidates
            ]

        processor = CommentProcessor(
            graph_client=GraphClient(
                access_token=settings.meta_page_access_token,
                http_client=http_client,
                api_version=settings.graph_api_version,
            ),
            store=processed_store,
            outbound_store=OutboundMessageStore(database_path),
            rule_store=rule_store,
            page_id=settings.meta_page_id,
            keyword=settings.resource_keyword,
            resource_url=settings.resource_url,
            public_reply_text=settings.public_reply_text,
        )
        results = await processor.process_events(
            [
                CommentEvent(
                    platform=candidate.platform,
                    comment_id=candidate.comment_id,
                    text=candidate.text,
                    media_id=candidate.media_id,
                    username=candidate.username,
                    parent_id=candidate.parent_id,
                )
                for candidate in candidates
            ]
        )
        result_by_id = {result.comment_id: result.status for result in results}
        return [
            {
                "platform": candidate.platform,
                "comment_id": candidate.comment_id,
                "media_id": candidate.media_id,
                "username": candidate.username,
                "text": candidate.text,
                "status": result_by_id.get(candidate.comment_id, "unknown"),
            }
            for candidate in candidates
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill untreated Meta comments matching active campaign rules.")
    parser.add_argument("--platform", choices=["instagram", "facebook", "all"], default="instagram")
    parser.add_argument("--media-limit", type=int, default=25, help="Recent IG media / FB posts to inspect")
    parser.add_argument("--comments-limit", type=int, default=500, help="Comments to inspect per media/post")
    parser.add_argument(
        "--comment-id",
        action="append",
        default=[],
        help="Only process this exact comment ID. Can be passed multiple times.",
    )
    parser.add_argument("--db", default="data/processed_comments.sqlite3")
    parser.add_argument("--apply", action="store_true", help="Send public replies and DMs. Omit for dry-run.")
    args = parser.parse_args()

    rows = asyncio.run(
        run_backfill(
            apply=args.apply,
            platform=args.platform,
            media_limit=args.media_limit,
            comments_limit=args.comments_limit,
            database_path=args.db,
            comment_ids=set(args.comment_id),
        )
    )
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: {len(rows)} matching untreated comment(s)")
    for row in rows:
        username = row.get("username") or "unknown"
        text = (row.get("text") or "").replace("\n", " ")[:120]
        print(f"- [{row['status']}] {row['platform']} {username} comment_id={row['comment_id']} media_id={row.get('media_id')} text={text}")


if __name__ == "__main__":
    main()
