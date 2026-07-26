from app.comment_review_store import CommentReviewStore
from app.platform_utils import is_owner_comment_event
from app.webhook_parser import CommentEvent


def reconcile_manual_review_replies(
    *,
    events: list[CommentEvent],
    review_store: CommentReviewStore,
    owner_ids: set[str],
    owner_usernames: set[str],
) -> int:
    count = 0
    for event in events:
        if not is_owner_comment_event(event, owner_ids, owner_usernames):
            continue
        target_comment_id = event.parent_id or event.comment_id
        item = review_store.get(event.platform, target_comment_id)
        if item is not None and item.status in {"pending", "in_review"}:
            review_store.mark_manually_replied(
                event.platform,
                target_comment_id,
                owner_reply_id=event.comment_id,
            )
            count += 1
        if event.parent_id:
            count += review_store.mark_thread_replies_manually_replied(
                event.platform,
                event.parent_id,
                owner_reply_id=event.comment_id,
            )
    return count
