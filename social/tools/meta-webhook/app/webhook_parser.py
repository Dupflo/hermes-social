from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CommentEvent:
    platform: str
    comment_id: str
    text: str
    media_id: str | None = None
    author_id: str | None = None
    username: str | None = None
    parent_id: str | None = None


@dataclass(frozen=True)
class PrivateMessageEvent:
    platform: str
    message_id: str
    text: str
    sender_id: str | None = None
    recipient_id: str | None = None
    timestamp: int | None = None


def parse_comment_events(payload: dict[str, Any]) -> list[CommentEvent]:
    platform = _platform_from_object(payload.get("object"))
    events: list[CommentEvent] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            event = _parse_change(platform, change)
            if event is not None:
                events.append(event)

    return events


def parse_private_message_events(payload: dict[str, Any]) -> list[PrivateMessageEvent]:
    platform = "facebook_dm" if payload.get("object") == "page" else "instagram_dm"
    events: list[PrivateMessageEvent] = []

    for entry in payload.get("entry", []):
        for message in entry.get("messaging", []):
            message_body = message.get("message") or {}
            text = message_body.get("text")
            message_id = message_body.get("mid") or message_body.get("id")
            if not message_id or text is None:
                continue
            sender = message.get("sender") or {}
            recipient = message.get("recipient") or {}
            events.append(
                PrivateMessageEvent(
                    platform=platform,
                    message_id=message_id,
                    text=text,
                    sender_id=sender.get("id"),
                    recipient_id=recipient.get("id"),
                    timestamp=message.get("timestamp"),
                )
            )
    return events


def _platform_from_object(object_name: str | None) -> str:
    if object_name == "page":
        return "facebook"
    return "instagram"


def _parse_change(platform: str, change: dict[str, Any]) -> CommentEvent | None:
    field = change.get("field")
    value = change.get("value", {})

    if platform == "instagram" and field in {"comments", "live_comments"}:
        comment_id = value.get("id") or value.get("comment_id")
        text = value.get("text") or value.get("message")
        if not comment_id or text is None:
            return None
        author = value.get("from", {}) or {}
        return CommentEvent(
            platform="instagram",
            comment_id=comment_id,
            text=text,
            media_id=value.get("media_id"),
            author_id=author.get("id"),
            username=author.get("username") or author.get("name"),
            parent_id=value.get("parent_id") or value.get("parent_comment_id"),
        )

    if platform == "facebook" and field == "feed" and value.get("item") == "comment":
        if value.get("verb") and value.get("verb") != "add":
            return None
        comment_id = value.get("comment_id") or value.get("id")
        text = value.get("message") or value.get("text")
        if not comment_id or text is None:
            return None
        author = value.get("from", {}) or {}
        return CommentEvent(
            platform="facebook",
            comment_id=comment_id,
            text=text,
            media_id=value.get("post_id"),
            author_id=author.get("id"),
            username=author.get("name") or author.get("username"),
            parent_id=value.get("parent_id") or value.get("parent_comment_id"),
        )

    return None
