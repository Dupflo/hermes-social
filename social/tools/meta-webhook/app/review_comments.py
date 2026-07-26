import argparse
import asyncio
from typing import Protocol

import httpx

from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.config import get_settings
from app.graph_client import GraphAPIError, GraphClient
from app.outbound_message_store import OutboundMessageStore
from app.platform_utils import facebook_graph_comment_id, platform_label


class PublicReplyGraphClient(Protocol):
    async def reply_to_facebook_comment(self, comment_id: str, message: str) -> dict: ...

    async def reply_to_instagram_comment(self, comment_id: str, message: str) -> dict: ...

    async def send_direct_message(self, recipient_id: str, text: str) -> dict: ...


def select_next_item(store: CommentReviewStore) -> CommentReviewItem | None:
    active = store.active_in_review()
    if active:
        return active
    item = store.next_pending()
    if item is None:
        return None
    store.mark_in_review(item.platform, item.comment_id)
    return store.get(item.platform, item.comment_id)


def format_next_item(item: CommentReviewItem | None, *, pending_total: int | None = None) -> str:
    if item is None:
        return "Aucun commentaire intéressant en attente ✅"
    total_text = f"{pending_total} commentaire intéressant" if pending_total == 1 else f"{pending_total} commentaires intéressants"
    if pending_total is None:
        total_text = "Commentaire intéressant"
    platform = platform_label(item.platform)
    permalink = item.comment_permalink or item.media_permalink or "Lien non disponible"
    return (
        f"{total_text} à traiter.\n\n"
        f"Plateforme: {platform}\n"
        f"Auteur: {item.username or 'unknown'}\n"
        f"Raison: {item.reason}\n"
        f"Vidéo: {permalink}\n"
        f"Comment ID: {item.comment_id}\n\n"
        f"Commentaire:\n\"{item.text}\"\n\n"
        "Réponds directement avec le message à poster, dis `skip`, ou dis `lien` pour ouvrir le commentaire."
    )


def format_link_context(context: dict[str, str | None]) -> str:
    link = context.get("comment_permalink") or context.get("media_permalink") or "Lien non disponible"
    return (
        f"Lien vidéo/commentaire : {link}\n"
        f"Comment ID : {context.get('comment_id')}\n"
        f"Auteur : {context.get('username') or 'unknown'}"
    )


async def post_review_reply(
    store: CommentReviewStore,
    graph_client: PublicReplyGraphClient,
    *,
    platform: str,
    comment_id: str,
    text: str,
    outbound_store: OutboundMessageStore | None = None,
) -> str:
    item = store.get(platform, comment_id)
    if item is None:
        return "Commentaire introuvable dans la file."
    if item.status == "manually_replied":
        return "Déjà répondu manuellement sur la plateforme."
    if item.status == "replied":
        return "Déjà répondu via Hermes."
    if platform.endswith("_dm"):
        if not item.username:
            return "Impossible de répondre au DM : identifiant destinataire manquant."
        try:
            result = await graph_client.send_direct_message(item.username, text)
        except GraphAPIError as error:
            store.mark_error(platform, comment_id, str(error))
            return f"Erreur Meta lors de la réponse DM : {error}"
        _record_outbound(
            outbound_store,
            platform=platform,
            source_id=comment_id,
            recipient_id=item.username,
            message_type="direct_message",
            message_text=text,
            result=result,
        )
        store.mark_replied(platform, comment_id, posted_reply_id=str(result.get("message_id") or result.get("id") or ""))
        return "Message privé envoyé ✅"
    recipient_id = comment_id
    try:
        if platform == "facebook":
            recipient_id = facebook_graph_comment_id(comment_id)
            result = await graph_client.reply_to_facebook_comment(recipient_id, text)
        elif platform == "instagram":
            result = await graph_client.reply_to_instagram_comment(comment_id, text)
        else:
            return f"Plateforme inconnue: {platform}"
    except GraphAPIError as error:
        store.mark_error(platform, comment_id, str(error))
        return f"Erreur Meta lors de la réponse : {error}"
    _record_outbound(
        outbound_store,
        platform=platform,
        source_id=comment_id,
        recipient_id=recipient_id,
        message_type="public_reply",
        message_text=text,
        result=result,
    )
    store.mark_replied(platform, comment_id, posted_reply_id=str(result.get("id") or result.get("message_id") or ""))
    return "Réponse postée ✅"


def _record_outbound(
    outbound_store: OutboundMessageStore | None,
    *,
    platform: str,
    source_id: str,
    recipient_id: str | None,
    message_type: str,
    message_text: str,
    result: dict,
) -> None:
    if outbound_store is None:
        return
    outbound_store.record_sent(
        platform=platform,
        source_type="manual_review",
        source_id=source_id,
        recipient_id=recipient_id,
        message_type=message_type,
        message_text=message_text,
        meta_response_id=str(result.get("message_id") or result.get("id") or "") or None,
    )


def skip_review_item(store: CommentReviewStore, platform: str, comment_id: str) -> str:
    if store.get(platform, comment_id) is None:
        return "Commentaire introuvable dans la file."
    store.mark_skipped(platform, comment_id)
    return "Commentaire ignoré ✅"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review interesting Meta comments one by one")
    parser.add_argument("--db", default="data/processed_comments.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("count")
    sub.add_parser("next")
    link = sub.add_parser("link")
    link.add_argument("--platform", required=True, choices=["facebook", "instagram", "facebook_dm", "instagram_dm"])
    link.add_argument("--comment-id", required=True)
    reply = sub.add_parser("reply")
    reply.add_argument("--platform", required=True, choices=["facebook", "instagram", "facebook_dm", "instagram_dm"])
    reply.add_argument("--comment-id", required=True)
    reply.add_argument("--text", required=True)
    skip = sub.add_parser("skip")
    skip.add_argument("--platform", required=True, choices=["facebook", "instagram", "facebook_dm", "instagram_dm"])
    skip.add_argument("--comment-id", required=True)
    return parser


async def async_main(argv: list[str] | None = None) -> str:
    args = build_parser().parse_args(argv)
    store = CommentReviewStore(args.db)
    if args.command == "count":
        counts = store.counts_by_status()
        return "\n".join(f"{status}: {count}" for status, count in sorted(counts.items())) or "empty"
    if args.command == "next":
        item = select_next_item(store)
        pending_total = store.counts_by_status().get("pending", 0) + (1 if item else 0)
        return format_next_item(item, pending_total=pending_total)
    if args.command == "link":
        return format_link_context(store.link_context(args.platform, args.comment_id))
    if args.command == "skip":
        return skip_review_item(store, args.platform, args.comment_id)
    if args.command == "reply":
        settings = get_settings()
        async with httpx.AsyncClient(timeout=30) as http_client:
            graph = GraphClient(
                access_token=settings.meta_page_access_token,
                http_client=http_client,
                api_version=settings.graph_api_version,
            )
            return await post_review_reply(
                store,
                graph,
                platform=args.platform,
                comment_id=args.comment_id,
                text=args.text,
                outbound_store=OutboundMessageStore(args.db),
            )
    raise AssertionError(f"Unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> None:
    print(asyncio.run(async_main(argv)))


if __name__ == "__main__":
    main()
