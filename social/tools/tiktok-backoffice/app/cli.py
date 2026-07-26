from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.keyword import contains_keyword
from app.models import Campaign, ReplyDraft, TikTokComment
from app.store import TikTokBackofficeStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Draft-only TikTok backoffice")
    parser.add_argument("--db", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

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

    campaign = sub.add_parser("campaign-upsert")
    campaign.add_argument("--slug", required=True)
    campaign.add_argument("--name", required=True)
    campaign.add_argument("--keywords", required=True, help="Comma-separated keyword list")
    campaign.add_argument("--reply", required=True)

    match = sub.add_parser("match")
    match.add_argument("--campaign", required=True)

    sub.add_parser("next-review")

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


    if args.command == "campaign-upsert":
        keywords = tuple(keyword.strip() for keyword in args.keywords.split(",") if keyword.strip())
        store.upsert_campaign(Campaign(slug=args.slug, name=args.name, keywords=keywords, reply_template=args.reply))
        return json.dumps({"ok": True, "slug": args.slug}, ensure_ascii=False)

    if args.command == "match":
        result = store.match_campaign(args.campaign)
        return json.dumps({"ok": True, **result}, ensure_ascii=False)

    if args.command == "next-review":
        return json.dumps({"ok": True, "item": store.next_review_item()}, ensure_ascii=False)

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
