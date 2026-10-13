"""s09 — one construction of the CommentProcessor, shared by the webhook and the replay.

The backfill builds its processor without the follow gate nor the owner identities; the
replay must not diverge from the webhook, so both go through this factory.
"""

from app.campaign_rules import CampaignRuleStore
from app.config import Settings
from app.follow_gate_store import FollowGateStore
from app.outbound_message_store import OutboundMessageStore
from app.platform_utils import owner_identity_sets
from app.processor import CommentProcessor
from app.store import ProcessedCommentStore


def build_comment_processor(settings: Settings, graph_client) -> CommentProcessor:
    database = settings.processed_comments_database
    owner_ids, owner_usernames = owner_identity_sets(settings)
    return CommentProcessor(
        graph_client=graph_client,
        store=ProcessedCommentStore(database),
        outbound_store=OutboundMessageStore(database),
        rule_store=CampaignRuleStore(database),
        follow_gate_store=FollowGateStore(database),
        page_id=settings.meta_page_id,
        keyword=settings.resource_keyword,
        resource_url=settings.resource_url,
        public_reply_text=settings.public_reply_text,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
        instagram_messaging_enabled=bool(settings.instagram_app_secret),
    )
