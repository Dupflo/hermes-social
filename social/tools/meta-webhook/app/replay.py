"""s09 — rebuild a CommentEvent from Graph to replay a failed or stuck comment.

The console's ADR 031 is the contract: the event is always read back from Graph
(processed_comments has no text, media nor author), Instagram without an author id
is refused (a follow_gate rule would otherwise send the resource without the gate),
and Meta's 7-day private-reply window is checked on the Graph date, strictly.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from app.webhook_parser import CommentEvent


META_WINDOW = timedelta(days=7)
GRAPH_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def facebook_media_id(comment_id: str, page_id: str) -> str | None:
    """`<page>_<post>` from a compound `<post>_<comment>` id, like the webhook's `post_id`."""
    if "_" not in comment_id or not page_id:
        return None
    prefix = comment_id.split("_", 1)[0]
    return f"{page_id}_{prefix}" if prefix else None


def build_replay_event(platform: str, comment_id: str, data: dict[str, Any], page_id: str) -> CommentEvent | None:
    """Return the event to replay, or None when Graph did not give enough to process it safely."""
    author = data.get("from") if isinstance(data.get("from"), dict) else {}
    author_id = author.get("id") or None

    if platform == "instagram":
        text = data.get("text")
        if not isinstance(text, str) or not author_id:
            return None
        media = data.get("media") if isinstance(data.get("media"), dict) else {}
        return CommentEvent(
            platform="instagram",
            comment_id=comment_id,
            text=text,
            media_id=media.get("id") or None,
            author_id=author_id,
            username=data.get("username") or author.get("username") or None,
        )

    if platform == "facebook":
        text = data.get("message")
        if not isinstance(text, str):
            return None
        return CommentEvent(
            platform="facebook",
            comment_id=comment_id,
            text=text,
            media_id=facebook_media_id(comment_id, page_id),
            author_id=author_id,
            username=author.get("name") or author.get("username") or None,
        )

    return None


def graph_comment_age_exceeded(data: dict[str, Any], platform: str, now: datetime) -> bool | None:
    """True if the comment is strictly older than 7 days, None if its date is missing or unreadable."""
    field = "timestamp" if platform == "instagram" else "created_time"
    raw = data.get(field)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        created = datetime.strptime(raw, GRAPH_DATE_FORMAT)
    except ValueError:
        return None
    return now - created > META_WINDOW
