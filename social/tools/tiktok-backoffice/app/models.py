from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReviewStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    DRAFTED = "drafted_in_browser"
    MANUAL_CONFIRMED = "manual_confirmed"
    POSTED = "posted"
    FAILED = "failed"
    IGNORED = "ignored"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class TikTokComment:
    video_url: str
    comment_id: str
    author: str | None
    text: str
    video_id: str | None = None


@dataclass(frozen=True)
class ReplyDraft:
    comment_id: str
    keyword: str
    reply_text: str
