import logging
from dataclasses import dataclass
from typing import Protocol

from app.campaign_rules import CampaignRule
from app.campaign_rules import CampaignRuleStore
from app.follow_gate_store import FollowGateStore
from app.follow_gate_store import follow_gate_prompt_text
from app.follow_gate_store import follow_gate_quick_replies
from app.follow_gate_store import is_follow_gate_confirmation
from app.graph_client import GraphAPIError
from app.keyword import contains_keyword
from app.outbound_message_store import OutboundMessageStore
from app.platform_utils import facebook_graph_comment_id
from app.platform_utils import is_owner_comment_event
from app.store import ProcessedCommentStore
from app.webhook_parser import CommentEvent
from app.webhook_parser import PrivateMessageEvent


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
        quick_replies: list[dict[str, str]] | None = None,
    ) -> dict: ...

    async def private_reply_to_facebook_comment(self, page_id: str, comment_id: str, text: str) -> dict: ...

    async def can_reply_privately_to_facebook_comment(self, comment_id: str) -> bool: ...

    async def send_direct_message(
        self,
        recipient_id: str,
        text: str,
        quick_replies: list[dict[str, str]] | None = None,
    ) -> dict: ...


FACEBOOK_PRIVATE_REPLY_FALLBACK_TEXT = (
    "Meta ne me laisse pas t’écrire en premier 😅\n"
    "Envoie-moi un petit message et je t’envoie le lien."
)
FACEBOOK_DM_INVITATION_CONFIRMATION_TEXT = (
    "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger."
)
DEFAULT_DM_CONFIRMATION_TEXT = "C'est envoyé ! Check tes messages privés !"
FOLLOW_GATE_PUBLIC_REPLY_TEXT = "Je t’ai envoyé les instructions en privé. Réponds « ✅ J’ai suivi » dans le message privé et je t’envoie le lien."


def select_public_reply_variant(base_text: str, comment_id: str) -> str:
    variants = [part.strip() for part in (base_text or "").split("---VARIANT---") if part.strip()]
    if not variants:
        return DEFAULT_DM_CONFIRMATION_TEXT
    return variants[sum(ord(ch) for ch in comment_id) % len(variants)]


def public_reply_for_platform(platform: str, base_text: str, comment_id: str = "") -> str:
    text = select_public_reply_variant(base_text, comment_id).strip() or DEFAULT_DM_CONFIRMATION_TEXT
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
        follow_gate_store: FollowGateStore | None = None,
        page_id: str,
        keyword: str = "proxy",
        resource_url: str = "",
        public_reply_text: str = "C'est envoyé, check tes DM",
        owner_ids: set[str] | None = None,
        owner_usernames: set[str] | None = None,
        instagram_messaging_enabled: bool = False,
    ) -> None:
        self.graph_client = graph_client
        self.store = store
        self.outbound_store = outbound_store
        self.rule_store = rule_store
        self.follow_gate_store = follow_gate_store
        self.page_id = page_id
        self.keyword = keyword
        self.resource_url = resource_url
        self.public_reply_text = public_reply_text
        self.owner_ids = owner_ids or set()
        self.owner_usernames = owner_usernames or set()
        # The Instagram follow-gate unlock relies on receiving the user's DM reply,
        # which only verifies once the Instagram app secret is configured.
        self.instagram_messaging_enabled = instagram_messaging_enabled

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
        public_reply_text = (
            FOLLOW_GATE_PUBLIC_REPLY_TEXT
            if rule.delivery_mode == "follow_gate"
            else public_reply_for_platform(event.platform, rule.public_reply_text, event.comment_id)
        )
        dm_text = rule.dm_text.strip()
        if not dm_text:
            return ProcessingResult(comment_id=event.comment_id, status="ignored")

        if not self.store.try_claim(event.platform, event.comment_id, keyword):
            return ProcessingResult(comment_id=event.comment_id, status="duplicate")

        existing_state = self.store.delivery_state(event.platform, event.comment_id)
        already_liked = bool(existing_state and existing_state["like_sent"])
        already_public_replied = bool(existing_state and existing_state["public_reply_sent"])
        already_dm_sent = bool(existing_state and existing_state["dm_sent"])

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
                if rule.delivery_mode == "follow_gate" and self.follow_gate_store is not None:
                    if not can_send_private_reply:
                        if not already_public_replied:
                            result = await self.graph_client.reply_to_facebook_comment(
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
                    if not already_dm_sent:
                        prompt_text = follow_gate_prompt_text()
                        result = await self.graph_client.private_reply_to_instagram_comment(
                            page_id=self.page_id,
                            comment_id=event.comment_id,
                            text=prompt_text,
                            quick_replies=follow_gate_quick_replies(),
                        )
                        self._record_outbound(
                            platform=event.platform,
                            source_id=event.comment_id,
                            recipient_id=event.author_id or event.comment_id,
                            message_type="follow_gate_prompt",
                            message_text=prompt_text,
                            result=result,
                        )
                        self.follow_gate_store.create_pending(
                            platform=event.platform,
                            comment_id=event.comment_id,
                            sender_id=event.author_id or event.comment_id,
                            campaign=rule.source_task_id,
                            keyword=keyword,
                            dm_text=dm_text,
                        )
                        dm_sent = False
                    if not already_public_replied:
                        result = await self.graph_client.reply_to_facebook_comment(graph_comment_id, public_reply_text)
                        self._record_outbound(
                            platform=event.platform,
                            source_id=event.comment_id,
                            recipient_id=graph_comment_id,
                            message_type="public_reply",
                            message_text=public_reply_text,
                            result=result,
                        )
                        public_reply_sent = True
                    self.store.mark_follow_gate_pending(
                        platform=event.platform,
                        comment_id=event.comment_id,
                        keyword=keyword,
                        like_sent=like_sent,
                        public_reply_sent=public_reply_sent,
                    )
                    return ProcessingResult(comment_id=event.comment_id, status="follow_gate_pending")
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
                    if rule.delivery_mode == "follow_gate" and not self.instagram_messaging_enabled:
                        # Without the Instagram app secret, Instagram messaging
                        # webhooks fail signature checks, so we cannot observe the
                        # user replying "J'ai suivi". Do not send a follow-gate
                        # prompt that promises an automatic unlock.
                        self.store.mark_private_reply_blocked(
                            platform=event.platform,
                            comment_id=event.comment_id,
                            keyword=keyword,
                            like_sent=like_sent,
                            public_reply_sent=public_reply_sent,
                            error_message="instagram follow_gate disabled: INSTAGRAM_APP_SECRET not configured",
                        )
                        return ProcessingResult(comment_id=event.comment_id, status="instagram_follow_gate_disabled")
                    if rule.delivery_mode == "follow_gate" and self.follow_gate_store is not None and event.author_id:
                        prompt_text = follow_gate_prompt_text()
                        try:
                            result = await self.graph_client.private_reply_to_instagram_comment(
                                page_id=self.page_id,
                                comment_id=graph_comment_id,
                                text=prompt_text,
                                quick_replies=follow_gate_quick_replies(),
                            )
                            self._record_outbound(
                                platform=event.platform,
                                source_id=event.comment_id,
                                recipient_id=event.author_id,
                                message_type="follow_gate_prompt",
                                message_text=prompt_text,
                                result=result,
                            )
                            self.follow_gate_store.create_pending(
                                platform=event.platform,
                                comment_id=event.comment_id,
                                sender_id=event.author_id,
                                campaign=rule.source_task_id,
                                keyword=keyword,
                                dm_text=dm_text,
                            )
                            return_status = "follow_gate_pending"
                        except GraphAPIError as error:
                            if not error.blocks_private_reply:
                                raise
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
                                error_message=str(error),
                            )
                            return ProcessingResult(comment_id=event.comment_id, status="private_reply_blocked")
                    else:
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
                        except GraphAPIError as error:
                            if not error.blocks_private_reply:
                                raise
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
                                error_message=str(error),
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
                if 'return_status' in locals() and return_status == "follow_gate_pending":
                    self.store.mark_follow_gate_pending(
                        platform=event.platform,
                        comment_id=event.comment_id,
                        keyword=keyword,
                        like_sent=like_sent,
                        public_reply_sent=public_reply_sent,
                    )
                    return ProcessingResult(comment_id=event.comment_id, status="follow_gate_pending")
        except GraphAPIError as error:
            logger.exception("Meta Graph API action failed for comment_id=%s", event.comment_id)
            self.store.mark_failed(
                platform=event.platform,
                comment_id=event.comment_id,
                keyword=keyword,
                error_message=str(error),
            )
            return ProcessingResult(comment_id=event.comment_id, status="failed_graph_api")
        except Exception as error:
            logger.exception("Unexpected error processing comment_id=%s", event.comment_id)
            self.store.mark_failed(
                platform=event.platform,
                comment_id=event.comment_id,
                keyword=keyword,
                error_message=f"{type(error).__name__}: {error}"[:500],
            )
            return ProcessingResult(comment_id=event.comment_id, status="failed_unexpected")

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


@dataclass(frozen=True)
class PrivateMessageProcessingResult:
    message_id: str
    status: str


class PrivateMessageProcessor:
    def __init__(
        self,
        *,
        graph_client: CommentGraphClient,
        follow_gate_store: FollowGateStore,
        processed_store: ProcessedCommentStore,
        outbound_store: OutboundMessageStore | None = None,
        page_id: str = "",
    ) -> None:
        self.graph_client = graph_client
        self.follow_gate_store = follow_gate_store
        self.processed_store = processed_store
        self.outbound_store = outbound_store
        self.page_id = page_id

    async def process_events(self, events: list[PrivateMessageEvent]) -> list[PrivateMessageProcessingResult]:
        results = []
        for event in events:
            results.append(await self._process_event(event))
        return results

    async def _process_event(self, event: PrivateMessageEvent) -> PrivateMessageProcessingResult:
        if event.platform not in {"instagram_dm", "facebook_dm"} or not event.sender_id:
            return PrivateMessageProcessingResult(event.message_id, "ignored")
        if not is_follow_gate_confirmation(event.payload or event.text):
            return PrivateMessageProcessingResult(event.message_id, "ignored")
        gate_platform = "facebook" if event.platform == "facebook_dm" else "instagram"
        gate = self.follow_gate_store.pending_for_sender(gate_platform, event.sender_id)
        if gate is None:
            return PrivateMessageProcessingResult(event.message_id, "no_pending_follow_gate")
        try:
            # Without Advanced Access to platform messaging, Meta may reject a normal
            # DM to the user ID. A second private reply scoped to the original
            # comment is still allowed inside the comment private-reply window, so
            # use recipient.comment_id here for both Instagram and Facebook gates.
            result = await self.graph_client.private_reply_to_instagram_comment(
                self.page_id or event.recipient_id or "me",
                gate.comment_id,
                gate.dm_text,
            )
        except GraphAPIError as error:
            logger.exception("Failed to unlock follow gate for sender_id=%s", event.sender_id)
            return PrivateMessageProcessingResult(event.message_id, f"failed_graph_api:{error}")
        if self.outbound_store is not None:
            self.outbound_store.record_sent(
                platform=gate_platform,
                source_type="follow_gate",
                source_id=gate.comment_id,
                recipient_id=event.sender_id,
                message_type="private_reply",
                message_text=gate.dm_text,
                meta_response_id=str(result.get("message_id") or result.get("id") or "") or None,
            )
        self.follow_gate_store.mark_unlocked(gate_platform, gate.comment_id)
        state = self.processed_store.delivery_state(gate_platform, gate.comment_id) or {}
        self.processed_store.mark_processed(
            platform=gate_platform,
            comment_id=gate.comment_id,
            keyword=gate.keyword,
            like_sent=bool(state.get("like_sent")),
            public_reply_sent=bool(state.get("public_reply_sent")),
            dm_sent=True,
        )
        return PrivateMessageProcessingResult(event.message_id, "follow_gate_unlocked")
