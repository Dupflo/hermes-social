from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.discovery import discover_profile_videos
from app.keyword import contains_keyword
from app.kanban_import import DEFAULT_KANBAN_DB, sync_meta_campaigns_to_tiktok
from app.models import Campaign, ReplyDraft, ReviewItemStatus, TikTokComment, TikTokVideo
from app.store import TikTokBackofficeStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draft-only TikTok backoffice")
    parser.add_argument("--db", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)



    discover = sub.add_parser("discover-videos")
    discover.add_argument("--profile", required=True)
    discover.add_argument("--html-file")

    add_video = sub.add_parser("add-video")
    add_video.add_argument("--video-url", required=True)
    add_video.add_argument("--video-id")
    add_video.add_argument("--author")
    add_video.add_argument("--caption")

    list_videos = sub.add_parser("list-videos")
    list_videos.add_argument("--with-campaigns", action="store_true")
    list_videos.add_argument("--limit", type=int, default=50)

    assign_video = sub.add_parser("assign-video")
    assign_video.add_argument("--video-url", required=True)
    assign_video.add_argument("--campaign", required=True)
    assign_video.add_argument("--source", default="manual")
    assign_video.add_argument("--confidence", type=float, default=1.0)

    sub.add_parser("suggest-video-campaigns")

    approve_video_campaign = sub.add_parser("approve-video-campaign")
    approve_video_campaign.add_argument("--video-url", required=True)
    approve_video_campaign.add_argument("--campaign", required=True)


    poll_targets = sub.add_parser("poll-targets")
    poll_targets.add_argument("--limit", type=int, default=50)


    ingest = sub.add_parser("ingest-comments")
    ingest.add_argument("--video-url", required=True)
    ingest.add_argument("--json-file", type=Path, required=True)

    add = sub.add_parser("add-comment")
    add.add_argument("--video-url", required=True)
    add.add_argument("--video-id")
    add.add_argument("--comment-id", required=True)
    add.add_argument("--author")
    add.add_argument("--text", required=True)

    draft = sub.add_parser("draft")
    draft.add_argument("--comment-id", required=True)
    draft.add_argument("--keyword", required=True)
    draft.add_argument("--reply", required=True)

    captcha = sub.add_parser("captcha-needed")
    captcha.add_argument("--video-url", required=True)
    captcha.add_argument("--screenshot-path")

    browser_draft = sub.add_parser("browser-draft-filled")
    browser_draft.add_argument("--video-url", required=True)
    browser_draft.add_argument("--screenshot-path")


    sync_kanban = sub.add_parser("sync-kanban-campaigns")
    sync_kanban.add_argument("--kanban-db", type=Path, default=DEFAULT_KANBAN_DB)

    sub.add_parser("list-campaigns")

    campaign = sub.add_parser("campaign-upsert")
    campaign.add_argument("--slug", required=True)
    campaign.add_argument("--name", required=True)
    campaign.add_argument("--keywords", required=True, help="Comma-separated keyword list")
    campaign.add_argument("--reply", required=True)

    match = sub.add_parser("match")
    match.add_argument("--campaign", required=True)

    sub.add_parser("next-review")

    approve = sub.add_parser("approve-draft")
    approve.add_argument("--review-id", type=int, required=True)

    ignore = sub.add_parser("ignore-review")
    ignore.add_argument("--review-id", type=int, required=True)
    ignore.add_argument("--reason", default=None)

    review = sub.add_parser("review")
    review.add_argument("--review-id", type=int, required=True)

    sub.add_parser("next-browser-draft")

    browser_drafted = sub.add_parser("browser-drafted")
    browser_drafted.add_argument("--review-id", type=int, required=True)
    browser_drafted.add_argument("--screenshot-path")

    sub.add_parser("browser-events")
    sub.add_parser("next")
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--limit", type=int, default=20)
    list_cmd.add_argument("--status")
    return parser


def run(argv: list[str] | None = None) -> str:
    args = build_parser().parse_args(argv)
    settings = Settings()
    store = TikTokBackofficeStore(args.db or settings.tiktok_backoffice_db)



    if args.command == "discover-videos":
        videos = discover_profile_videos(profile=args.profile, html_file=args.html_file)
        added = sum(1 for video in videos if store.add_video(video))
        return json.dumps({"ok": True, "profile": args.profile if args.profile.startswith("@") else "@" + args.profile, "found": len(videos), "added": added}, ensure_ascii=False)

    if args.command == "add-video":
        changed = store.add_video(TikTokVideo(video_url=args.video_url, video_id=args.video_id, author=args.author, caption=args.caption))
        return json.dumps({"ok": True, "changed": changed}, ensure_ascii=False)

    if args.command == "list-videos":
        return json.dumps({"ok": True, "items": store.list_videos(with_campaigns=args.with_campaigns, limit=args.limit)}, ensure_ascii=False)

    if args.command == "assign-video":
        changed = store.assign_video_campaign(video_url=args.video_url, campaign_slug=args.campaign, source=args.source, confidence=args.confidence)
        return json.dumps({"ok": True, "changed": changed}, ensure_ascii=False)


    if args.command == "suggest-video-campaigns":
        return json.dumps({"ok": True, "suggested": store.suggest_video_campaigns()}, ensure_ascii=False)

    if args.command == "approve-video-campaign":
        changed = store.approve_video_campaign(video_url=args.video_url, campaign_slug=args.campaign)
        return json.dumps({"ok": True, "changed": changed}, ensure_ascii=False)


    if args.command == "poll-targets":
        return json.dumps({"ok": True, "items": store.poll_targets(limit=args.limit)}, ensure_ascii=False)


    if args.command == "ingest-comments":
        payload = json.loads(args.json_file.read_text())
        comments = payload.get("comments", payload) if isinstance(payload, dict) else payload
        if not isinstance(comments, list):
            return json.dumps({"ok": False, "error": "json_must_be_list_or_comments_object"}, ensure_ascii=False)
        result = store.ingest_comments(video_url=args.video_url, comments=comments)
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    if args.command == "add-comment":
        changed = store.add_comment(TikTokComment(video_url=args.video_url, video_id=args.video_id, comment_id=args.comment_id, author=args.author, text=args.text))
        return json.dumps({"ok": True, "changed": changed}, ensure_ascii=False)

    if args.command == "draft":
        comment = store.get_comment(args.comment_id)
        if comment is None:
            return json.dumps({"ok": False, "error": "unknown_comment"}, ensure_ascii=False)
        if not contains_keyword(comment["text"], args.keyword):
            return json.dumps({"ok": False, "error": "keyword_not_found", "comment_text": comment["text"]}, ensure_ascii=False)
        if settings.publish_enabled:
            return json.dumps({"ok": False, "error": "publishing_not_implemented"}, ensure_ascii=False)
        store.save_draft(ReplyDraft(comment_id=args.comment_id, keyword=args.keyword, reply_text=args.reply))
        return json.dumps({"ok": True, "mode": "draft_only", "comment_id": args.comment_id, "reply_text": args.reply, "message": "Draft saved locally only; nothing was posted to TikTok."}, ensure_ascii=False)

    if args.command == "captcha-needed":
        store.mark_needs_manual_captcha(video_url=args.video_url, screenshot_path=args.screenshot_path)
        return json.dumps(
            {
                "ok": True,
                "status": "needs_manual_captcha",
                "message": "TikTok showed a slider CAPTCHA; solve it manually via Camofox/noVNC before browser drafting.",
            },
            ensure_ascii=False,
        )

    if args.command == "browser-draft-filled":
        store.mark_browser_draft_filled(video_url=args.video_url, screenshot_path=args.screenshot_path)
        return json.dumps(
            {
                "ok": True,
                "status": "browser_draft_filled_not_posted",
                "message": "Browser reply draft was filled and captured; nothing was posted to TikTok.",
            },
            ensure_ascii=False,
        )



    if args.command == "sync-kanban-campaigns":
        synced = sync_meta_campaigns_to_tiktok(store, args.kanban_db)
        return json.dumps({"ok": True, "synced": synced}, ensure_ascii=False)

    if args.command == "list-campaigns":
        return json.dumps({"ok": True, "items": store.list_campaigns()}, ensure_ascii=False)

    if args.command == "campaign-upsert":
        keywords = tuple(keyword.strip() for keyword in args.keywords.split(",") if keyword.strip())
        store.upsert_campaign(Campaign(slug=args.slug, name=args.name, keywords=keywords, reply_template=args.reply))
        return json.dumps({"ok": True, "slug": args.slug}, ensure_ascii=False)

    if args.command == "match":
        result = store.match_campaign(args.campaign)
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    if args.command == "next-review":
        return json.dumps({"ok": True, "item": store.next_review_item()}, ensure_ascii=False)


    if args.command == "approve-draft":
        store.set_review_status(args.review_id, ReviewItemStatus.APPROVED_FOR_DRAFT)
        return json.dumps({"ok": True, "review_id": args.review_id, "status": ReviewItemStatus.APPROVED_FOR_DRAFT}, ensure_ascii=False)

    if args.command == "ignore-review":
        store.set_review_status(args.review_id, ReviewItemStatus.IGNORED, reason=args.reason)
        return json.dumps({"ok": True, "review_id": args.review_id, "status": ReviewItemStatus.IGNORED}, ensure_ascii=False)

    if args.command == "review":
        return json.dumps({"ok": True, "item": store.get_review_item(args.review_id)}, ensure_ascii=False)


    if args.command == "next-browser-draft":
        return json.dumps({"ok": True, "item": store.next_browser_draft_item()}, ensure_ascii=False)

    if args.command == "browser-drafted":
        store.mark_review_browser_drafted(args.review_id, screenshot_path=args.screenshot_path)
        return json.dumps({"ok": True, "review_id": args.review_id, "status": ReviewItemStatus.DRAFTED_IN_BROWSER}, ensure_ascii=False)

    if args.command == "browser-events":
        return json.dumps({"ok": True, "items": store.recent_browser_events()}, ensure_ascii=False)

    if args.command == "next":
        return json.dumps({"ok": True, "item": store.next_pending()}, ensure_ascii=False)

    if args.command == "list":
        return json.dumps({"ok": True, "items": store.list_comments(limit=args.limit, status=args.status)}, ensure_ascii=False)

    raise AssertionError(f"Unhandled command: {args.command}")


def main() -> None:
    print(run())


if __name__ == "__main__":
    main()
