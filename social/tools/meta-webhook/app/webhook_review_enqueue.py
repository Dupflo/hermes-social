from app.backfill_comments import BackfillCandidate
from app.campaign_rules import CampaignRuleStore
from app.comment_review_scanner import MediaContext, ScanReviewSummary, scan_review_candidates
from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.platform_utils import is_owner_comment_event
from app.store import ProcessedCommentStore
from app.webhook_parser import CommentEvent, PrivateMessageEvent


def enqueue_interesting_webhook_comments(
    *,
    events: list[CommentEvent],
    review_store: CommentReviewStore,
    rule_store: CampaignRuleStore,
    processed_store: ProcessedCommentStore,
    owner_ids: set[str],
    owner_usernames: set[str],
    interest_only_keywords: set[str] | None = None,
) -> ScanReviewSummary:
    root_candidates = [
        BackfillCandidate(
            platform=event.platform,
            comment_id=event.comment_id,
            text=event.text,
            media_id=event.media_id,
            username=event.username,
        )
        for event in events
        if event.parent_id is None and not is_owner_comment_event(event, owner_ids, owner_usernames)
    ]
    summary = scan_review_candidates(
        comments=root_candidates,
        media_contexts={},
        rule_store=rule_store,
        processed_store=processed_store,
        review_store=review_store,
        interest_only_keywords=interest_only_keywords,
    )
    thread_inserted = _enqueue_thread_replies(
        events=events,
        review_store=review_store,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
    )
    if thread_inserted == 0:
        return summary
    counts = review_store.counts_by_status()
    return ScanReviewSummary(
        scanned_comments=summary.scanned_comments,
        inserted_pending=summary.inserted_pending + thread_inserted,
        manually_replied=summary.manually_replied,
        ignored=summary.ignored,
        pending_total=counts.get("pending", 0),
    )


def enqueue_private_message_events(
    *,
    events: list[PrivateMessageEvent],
    review_store: CommentReviewStore,
    owner_ids: set[str],
) -> int:
    inserted = 0
    for event in events:
        if event.sender_id and event.sender_id in owner_ids:
            continue
        if review_store.upsert_pending(
            CommentReviewItem(
                platform=event.platform,
                comment_id=event.message_id,
                media_id=event.recipient_id,
                username=event.sender_id,
                text=event.text,
                media_permalink=None,
                media_caption=None,
                reason="private_message",
                score=95,
            )
        ):
            inserted += 1
    return inserted


def _enqueue_thread_replies(
    *,
    events: list[CommentEvent],
    review_store: CommentReviewStore,
    owner_ids: set[str],
    owner_usernames: set[str],
) -> int:
    inserted = 0
    for event in events:
        if not event.parent_id:
            continue
        if is_owner_comment_event(event, owner_ids, owner_usernames):
            continue
        parent = review_store.get(event.platform, event.parent_id)
        if parent is None:
            continue
        if review_store.upsert_pending(
            CommentReviewItem(
                platform=event.platform,
                comment_id=event.comment_id,
                media_id=event.media_id or parent.media_id,
                username=event.username,
                text=event.text,
                media_permalink=parent.media_permalink,
                media_caption=parent.media_caption,
                reason="thread_reply",
                score=95,
                comment_permalink=parent.comment_permalink,
                parent_id=event.parent_id,
            )
        ):
            inserted += 1
    return inserted
