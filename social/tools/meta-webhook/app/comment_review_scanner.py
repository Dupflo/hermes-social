from dataclasses import dataclass

from app.backfill_comments import BackfillCandidate
from app.campaign_rules import CampaignRuleStore
from app.comment_review_classifier import CommentReviewDecision, classify_comment_for_review
from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.keyword import contains_keyword
from app.store import ProcessedCommentStore
from app.webhook_parser import CommentEvent


@dataclass(frozen=True)
class MediaContext:
    permalink: str | None = None
    caption: str | None = None
    comment_permalink: str | None = None


@dataclass(frozen=True)
class ScanReviewSummary:
    scanned_comments: int
    inserted_pending: int
    manually_replied: int
    ignored: int
    pending_total: int
    incomplete_media: list[dict[str, str | int | None]] | None = None


def scan_review_candidates(
    *,
    comments: list[BackfillCandidate],
    media_contexts: dict[tuple[str, str], MediaContext],
    rule_store: CampaignRuleStore,
    processed_store: ProcessedCommentStore,
    review_store: CommentReviewStore,
    interest_only_keywords: set[str] | None = None,
) -> ScanReviewSummary:
    inserted = 0
    manually_replied = 0
    ignored = 0

    for comment in comments:
        existing = review_store.get(comment.platform, comment.comment_id)
        if existing and comment.has_owner_reply and existing.status in {"pending", "in_review"}:
            review_store.mark_manually_replied(
                comment.platform,
                comment.comment_id,
                owner_reply_id=comment.owner_reply_id,
                owner_replied_at=comment.owner_reply_at,
            )
            manually_replied += 1
            continue

        event = CommentEvent(
            platform=comment.platform,
            comment_id=comment.comment_id,
            text=comment.text,
            media_id=comment.media_id,
            username=comment.username,
            parent_id=comment.parent_id,
        )
        matches_campaign = rule_store.find_matching_rule(event) is not None
        already_terminal = processed_store.is_terminal(comment.platform, comment.comment_id) or processed_store.is_fully_processed(
            comment.platform, comment.comment_id
        )
        if comment.parent_id and comment.has_owner_reply:
            ignored += 1
            continue
        context = media_contexts.get((comment.platform, comment.media_id or ""), MediaContext())
        if _is_interest_only_signal(comment.text, context.caption, interest_only_keywords or set()):
            ignored += 1
            continue
        if comment.parent_id and not already_terminal:
            decision = CommentReviewDecision(True, "thread_reply", 95)
        else:
            decision = classify_comment_for_review(
                text=comment.text,
                has_owner_reply=comment.has_owner_reply,
                matches_active_campaign=matches_campaign,
                already_terminal=already_terminal,
            )
        if not decision.should_store:
            ignored += 1
            continue

        was_inserted = review_store.upsert_pending(
            CommentReviewItem(
                platform=comment.platform,
                comment_id=comment.comment_id,
                media_id=comment.media_id,
                username=comment.username,
                text=comment.text,
                media_permalink=context.permalink,
                media_caption=context.caption,
                reason=decision.reason,
                score=decision.score,
                comment_permalink=context.comment_permalink,
                parent_id=comment.parent_id,
            )
        )
        if was_inserted:
            inserted += 1
        else:
            ignored += 1

    counts = review_store.counts_by_status()
    return ScanReviewSummary(
        scanned_comments=len(comments),
        inserted_pending=inserted,
        manually_replied=manually_replied,
        ignored=ignored,
        pending_total=counts.get("pending", 0),
    )


def _is_interest_only_signal(text: str, caption: str | None, keywords: set[str]) -> bool:
    for keyword in keywords:
        if contains_keyword(text, keyword) and (caption is None or contains_keyword(caption, keyword)):
            return True
    return False
