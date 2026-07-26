from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReviewItemStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    APPROVED_FOR_DRAFT = "approved_for_draft"
    DRAFTED_IN_BROWSER = "drafted_in_browser"
    APPROVED_FOR_PUBLISH = "approved_for_publish"
    POSTED = "posted"
    IGNORED = "ignored"
    FAILED = "failed"


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


@dataclass(frozen=True)
class Campaign:
    slug: str
    name: str
    keywords: tuple[str, ...]
    reply_template: str
    active: bool = True
