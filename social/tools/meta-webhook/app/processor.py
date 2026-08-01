import logging
from dataclasses import dataclass
from typing import Protocol

from app.campaign_rules import CampaignRule
from app.campaign_rules import CampaignRuleStore
from app.graph_client import GraphAPIError
from app.keyword import contains_keyword
from app.outbound_message_store import OutboundMessageStore
from app.platform_utils import facebook_graph_comment_id
from app.platform_utils import is_owner_comment_event
from app.store import ProcessedCommentStore
from app.webhook_parser import CommentEvent


logger = logging.getLogger(__name__)


class CommentGraphClient(Protocol):
    async def like_comment(self, comment_id: str) -> dict: ...

    async def reply_to_instagram_comment(self, comment_id: str, message: str) -> dict: ...

    async def reply_to_facebook_comment(self, comment_id: str, message: str) -> dict: ...

    async def private_reply_to_instagram_comment(
        self,
        page_id: str,
        comment_id: str,
        text: str,
    ) -> dict: ...

    async def private_reply_to_facebook_comment(self, page_id: str, comment_id: str, text: str) -> dict: ...

    async def can_reply_privately_to_facebook_comment(self, comment_id: str) -> bool: ...


FACEBOOK_PRIVATE_REPLY_FALLBACK_TEXT = (
    "Meta ne me laisse pas t’écrire en premier 😅\n"
    "Envoie-moi un petit message et je t’envoie le lien."
)
FACEBOOK_DM_INVITATION_CONFIRMATION_TEXT = (
    "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger."
)
DEFAULT_DM_CONFIRMATION_TEXT = "C'est envoyé ! Check tes messages privés !"


def public_reply_for_platform(platform: str, base_text: str) -> str:
    text = (base_text or DEFAULT_DM_CONFIRMATION_TEXT).strip() or DEFAULT_DM_CONFIRMATION_TEXT
    if platform == "facebook" and text.lower().startswith("c'est envoyé"):
        return FACEBOOK_DM_INVITATION_CONFIRMATION_TEXT
    if platform == "instagram" and "Messenger" in text:
        return DEFAULT_DM_CONFIRMATION_TEXT
    return text


@dataclass(frozen=True)
class ProcessingResult:
    comment_id: str
    status: str


class CommentProcessor:
    def __init__(
        self,
        *,
        graph_client: CommentGraphClient,
        store: ProcessedCommentStore,
        outbound_store: OutboundMessageStore | None = None,
        rule_store: CampaignRuleStore | None = None,
        page_id: str,
        keyword: str = "proxy",
        resource_url: str = "",
        public_reply_text: str = "C'est envoyé, check tes DM",
        owner_ids: set[str] | None = None,
        owner_usernames: set[str] | None = None,
    ) -> None:
        self.graph_client = graph_client
        self.store = store
        self.outbound_store = outbound_store
        self.rule_store = rule_store
        self.page_id = page_id
        self.keyword = keyword
        self.resource_url = resource_url
        self.public_reply_text = public_reply_text
        self.owner_ids = owner_ids or set()
        self.owner_usernames = owner_usernames or set()

    async def process_events(self, events: list[CommentEvent]) -> list[ProcessingResult]:
        results = []
        for event in events:
            results.append(await self._process_event(event))
        return results

    async def _process_event(self, event: CommentEvent) -> ProcessingResult:
        if is_owner_comment_event(event, self.owner_ids, self.owner_usernames):
            logger.info("Skipping owner comment event comment_id=%s platform=%s", event.comment_id, event.platform)
            return ProcessingResult(comment_id=event.comment_id, status="ignored_owner")

        rule = self.rule_store.find_matching_rule(event) if self.rule_store else None
        if rule is None:
            if self.rule_store and self.rule_store.has_enabled_rules():
                logger.info(
                    "No campaign rule matched comment_id=%s platform=%s media_id=%s; skipping legacy .env fallback",
                    event.comment_id,
                    event.platform,
                    event.media_id,
                )
                return ProcessingResult(comment_id=event.comment_id, status="ignored")
            if not contains_keyword(event.text, self.keyword):
                return ProcessingResult(comment_id=event.comment_id, status="ignored")
            rule = CampaignRule(
                source_task_id="legacy-env",
                name="Legacy .env campaign",
                platform="any",
                media_id=None,
                keywords=[self.keyword],
                public_reply_text=self.public_reply_text,
                dm_text=f"Voici la ressource demandée : {self.resource_url}",
                enabled=True,
            )

        keyword = rule.keywords[0]
        public_reply_text = public_reply_for_platform(event.platform, rule.public_reply_text)
        dm_text = rule.dm_text.strip()
        if not dm_text:
            return ProcessingResult(comment_id=event.comment_id, status="ignored")

        existing_state = self.store.delivery_state(event.platform, event.comment_id)
        already_liked = bool(existing_state and existing_state["like_sent"])
        already_public_replied = bool(existing_state and existing_state["public_reply_sent"])
        already_dm_sent = bool(existing_state and existing_state["dm_sent"])

        if not self.store.try_claim(event.platform, event.comment_id, keyword):
            return ProcessingResult(comment_id=event.comment_id, status="duplicate")

        like_sent = already_liked
        public_reply_sent = already_public_replied
        dm_sent = already_dm_sent
        graph_comment_id = self._graph_action_comment_id(event)

        try:
            if event.platform == "facebook" and not already_liked:
                try:
                    await self.graph_client.like_comment(graph_comment_id)
                    like_sent = True
                    self.store.mark_processed(
                        platform=event.platform,
                        comment_id=event.comment_id,
                        keyword=keyword,
                        like_sent=like_sent,
                        public_reply_sent=public_reply_sent,
                        dm_sent=dm_sent,
                    )
                except GraphAPIError:
                    logger.warning("Facebook like failed; continuing comment flow for comment_id=%s", event.comment_id)

            if event.platform == "facebook":
                can_send_private_reply = already_dm_sent or await self.graph_client.can_reply_privately_to_facebook_comment(
                    event.comment_id
                )
                if not already_public_replied:
                    reply_text = public_reply_text if can_send_private_reply else FACEBOOK_PRIVATE_REPLY_FALLBACK_TEXT
                    result = await self.graph_client.reply_to_facebook_comment(graph_comment_id, reply_text)
                    self._record_outbound(
                        platform=event.platform,
                        source_id=event.comment_id,
                        recipient_id=graph_comment_id,
                        message_type="public_reply",
                        message_text=reply_text,
                        result=result,
                    )
                    public_reply_sent = True
                    if not can_send_private_reply:
                        self.store.mark_private_reply_blocked(
                            platform=event.platform,
                            comment_id=event.comment_id,
                            keyword=keyword,
                            like_sent=like_sent,
                            public_reply_sent=public_reply_sent,
                        )
                        return ProcessingResult(comment_id=event.comment_id, status="private_reply_blocked")
                    self.store.mark_processed(
                        platform=event.platform,
                        comment_id=event.comment_id,
                        keyword=keyword,
                        like_sent=like_sent,
                        public_reply_sent=public_reply_sent,
                        dm_sent=dm_sent,
                    )
                if not already_dm_sent and can_send_private_reply:
                    result = await self.graph_client.private_reply_to_facebook_comment(self.page_id, event.comment_id, dm_text)
                    self._record_outbound(
                        platform=event.platform,
                        source_id=event.comment_id,
                        recipient_id=event.comment_id,
                        message_type="private_reply",
                        message_text=dm_text,
                        result=result,
                    )
                    dm_sent = True
                    self.store.mark_processed(
                        platform=event.platform,
                        comment_id=event.comment_id,
                        keyword=keyword,
                        like_sent=like_sent,
                        public_reply_sent=public_reply_sent,
                        dm_sent=dm_sent,
                    )
            else:
                if not already_dm_sent:
                    try:
                        result = await self.graph_client.private_reply_to_instagram_comment(
                            page_id=self.page_id,
                            comment_id=graph_comment_id,
                            text=dm_text,
                        )
                        self._record_outbound(
                            platform=event.platform,
                            source_id=event.comment_id,
                            recipient_id=graph_comment_id,
                            message_type="private_reply",
                            message_text=dm_text,
                            result=result,
                        )
                        dm_sent = True
                        self.store.mark_processed(
                            platform=event.platform,
                            comment_id=event.comment_id,
                            keyword=keyword,
                            like_sent=like_sent,
                            public_reply_sent=public_reply_sent,
                            dm_sent=dm_sent,
                        )
                    except GraphAPIError:
                        if not already_public_replied:
                            result = await self.graph_client.reply_to_instagram_comment(
                                graph_comment_id,
                                FACEBOOK_PRIVATE_REPLY_FALLBACK_TEXT,
                            )
                            self._record_outbound(
                                platform=event.platform,
                                source_id=event.comment_id,
                                recipient_id=graph_comment_id,
                                message_type="public_reply",
                                message_text=FACEBOOK_PRIVATE_REPLY_FALLBACK_TEXT,
                                result=result,
                            )
                            public_reply_sent = True
                        self.store.mark_private_reply_blocked(
                            platform=event.platform,
                            comment_id=event.comment_id,
                            keyword=keyword,
                            like_sent=like_sent,
                            public_reply_sent=public_reply_sent,
                        )
                        return ProcessingResult(comment_id=event.comment_id, status="private_reply_blocked")
                if not already_public_replied:
                    result = await self.graph_client.reply_to_instagram_comment(graph_comment_id, public_reply_text)
                    self._record_outbound(
                        platform=event.platform,
                        source_id=event.comment_id,
                        recipient_id=graph_comment_id,
                        message_type="public_reply",
                        message_text=public_reply_text,
                        result=result,
                    )
                    public_reply_sent = True
                    self.store.mark_processed(
                        platform=event.platform,
                        comment_id=event.comment_id,
                        keyword=keyword,
                        like_sent=like_sent,
                        public_reply_sent=public_reply_sent,
                        dm_sent=dm_sent,
                    )
        except GraphAPIError as error:
            logger.exception("Meta Graph API action failed for comment_id=%s", event.comment_id)
            self.store.mark_failed(
                platform=event.platform,
                comment_id=event.comment_id,
                keyword=keyword,
                error_message=str(error),
            )
            return ProcessingResult(comment_id=event.comment_id, status="failed_graph_api")

        self.store.mark_processed(
            platform=event.platform,
            comment_id=event.comment_id,
            keyword=keyword,
            like_sent=like_sent,
            public_reply_sent=public_reply_sent,
            dm_sent=dm_sent,
        )
        return ProcessingResult(comment_id=event.comment_id, status="processed")

    def _graph_action_comment_id(self, event: CommentEvent) -> str:
        return facebook_graph_comment_id(event.comment_id) if event.platform == "facebook" else event.comment_id

    def _record_outbound(
        self,
        *,
        platform: str,
        source_id: str,
        recipient_id: str | None,
        message_type: str,
        message_text: str,
        result: dict,
    ) -> None:
        if self.outbound_store is None:
            return
        self.outbound_store.record_sent(
            platform=platform,
            source_type="comment",
            source_id=source_id,
            recipient_id=recipient_id,
            message_type=message_type,
            message_text=message_text,
            meta_response_id=str(result.get("message_id") or result.get("id") or "") or None,
        )
