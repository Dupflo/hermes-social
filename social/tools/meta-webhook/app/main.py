import logging

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response

from app.campaign_rules import CampaignRuleStore
from app.config import get_settings
from app.graph_client import GraphClient
from app.outbound_message_store import OutboundMessageStore
from app.platform_utils import owner_identity_sets
from app.processor import CommentProcessor
from app.security import verify_meta_signature
from app.store import ProcessedCommentStore
from app.comment_review_store import CommentReviewStore
from app.webhook_manual_review import reconcile_manual_review_replies
from app.webhook_parser import parse_comment_events, parse_private_message_events
from app.webhook_review_enqueue import enqueue_interesting_webhook_comments, enqueue_private_message_events


logger = logging.getLogger(__name__)


PRIVACY_POLICY_HTML = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Politique de confidentialité - DupFloDev Lead Automation</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.6; max-width: 860px; margin: 40px auto; padding: 0 20px; color: #1f2937; }
    h1, h2 { color: #111827; }
    a { color: #2563eb; }
  </style>
</head>
<body>
  <h1>Politique de confidentialité</h1>
  <p><strong>Dernière mise à jour :</strong> 24 juillet 2026</p>

  <p>Cette application, <strong>DupFloDev Lead Automation</strong>, est éditée par FloDev / DupuisWeb. Elle permet d'automatiser certaines interactions liées aux commentaires Facebook et Instagram lorsqu'un utilisateur commente volontairement un mot-clé pour demander une ressource.</p>

  <h2>Données traitées</h2>
  <p>L'application peut traiter les données suivantes, uniquement lorsqu'elles sont fournies par Meta dans le cadre d'un commentaire ou d'un événement webhook :</p>
  <ul>
    <li>identifiant du commentaire ;</li>
    <li>contenu du commentaire ;</li>
    <li>identifiant de la publication ou du média concerné ;</li>
    <li>identifiant public ou nom d'utilisateur lorsque Meta le fournit ;</li>
    <li>statut technique du traitement afin d'éviter les doublons.</li>
  </ul>

  <h2>Finalité</h2>
  <p>Ces données sont utilisées uniquement pour détecter les commentaires contenant un mot-clé configuré, répondre au commentaire, envoyer la ressource demandée et éviter d'envoyer plusieurs fois le même message.</p>

  <h2>Partage et revente</h2>
  <p>Les données ne sont pas revendues. Elles ne sont pas utilisées pour du ciblage publicitaire.</p>

  <h2>Conservation</h2>
  <p>Les données techniques de traitement peuvent être conservées le temps nécessaire au bon fonctionnement du service, à la prévention des doublons et au diagnostic technique.</p>

  <h2>Sécurité</h2>
  <p>Les accès API, secrets et tokens sont stockés côté serveur et ne sont pas rendus publics.</p>

  <h2>Suppression des données</h2>
  <p>Vous pouvez demander la suppression de vos données liées à cette application via la page <a href="/data-deletion">Suppression des données</a> ou par email à <a href="mailto:contact@dupuisweb.com">contact@dupuisweb.com</a>.</p>

  <h2>Contact</h2>
  <p>Pour toute question : <a href="mailto:contact@dupuisweb.com">contact@dupuisweb.com</a></p>
</body>
</html>
"""


DATA_DELETION_HTML = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Suppression des données - DupFloDev Lead Automation</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.6; max-width: 860px; margin: 40px auto; padding: 0 20px; color: #1f2937; }
    h1, h2 { color: #111827; }
    a { color: #2563eb; }
  </style>
</head>
<body>
  <h1>Instructions de suppression des données</h1>
  <p><strong>Dernière mise à jour :</strong> 24 juillet 2026</p>

  <p>Si vous souhaitez demander la suppression des données associées à votre interaction avec l'application <strong>DupFloDev Lead Automation</strong>, vous pouvez nous contacter par email.</p>

  <h2>Comment demander la suppression</h2>
  <p>Envoyez un email à <a href="mailto:contact@dupuisweb.com">contact@dupuisweb.com</a> avec l'objet :</p>
  <p><strong>Demande de suppression des données - DupFloDev Lead Automation</strong></p>

  <p>Merci d'inclure, si possible, les informations permettant d'identifier votre demande :</p>
  <ul>
    <li>la Page Facebook ou le compte Instagram concerné ;</li>
    <li>la date approximative de votre commentaire ;</li>
    <li>le mot-clé ou le commentaire utilisé ;</li>
    <li>votre nom d'utilisateur public si nécessaire.</li>
  </ul>

  <h2>Délai de traitement</h2>
  <p>Nous traiterons les demandes de suppression dans un délai raisonnable après réception des informations nécessaires.</p>

  <h2>Contact</h2>
  <p><a href="mailto:contact@dupuisweb.com">contact@dupuisweb.com</a></p>
</body>
</html>
"""


META_AUTH_CALLBACK_HTML = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connexion Meta - DupFloDev Lead Automation</title>
</head>
<body>
  <h1>Connexion Meta reçue</h1>
  <p>La redirection Meta a bien atteint l'application. Vous pouvez fermer cette fenêtre.</p>
</body>
</html>
"""


def create_app() -> FastAPI:
    app = FastAPI(title="Meta Comment DM Automation")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/privacy")
    async def privacy_policy() -> Response:
        return Response(content=PRIVACY_POLICY_HTML, media_type="text/html")

    @app.get("/data-deletion")
    async def data_deletion() -> Response:
        return Response(content=DATA_DELETION_HTML, media_type="text/html")

    @app.get("/auth/meta/callback")
    async def meta_auth_callback() -> Response:
        return Response(content=META_AUTH_CALLBACK_HTML, media_type="text/html")

    @app.get("/auth/meta/deauthorize")
    async def meta_deauthorize_get() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/auth/meta/deauthorize")
    async def meta_deauthorize_post() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/webhook/meta")
    async def verify_webhook(
        hub_mode: str = Query(alias="hub.mode"),
        hub_verify_token: str = Query(alias="hub.verify_token"),
        hub_challenge: str = Query(alias="hub.challenge"),
    ) -> Response:
        settings = get_settings()
        if hub_mode == "subscribe" and hub_verify_token == settings.meta_verify_token:
            return Response(content=hub_challenge, media_type="text/plain")
        raise HTTPException(status_code=403, detail="Invalid verify token")

    @app.post("/webhook/meta")
    async def receive_webhook(request: Request) -> dict[str, int | str | list[dict[str, str]]]:
        settings = get_settings()
        raw_body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_meta_signature(raw_body, signature, settings.meta_app_secret):
            raise HTTPException(status_code=403, detail="Invalid Meta signature")

        payload = await request.json()
        events = parse_comment_events(payload)
        private_message_events = parse_private_message_events(payload)
        owner_ids, owner_usernames = owner_identity_sets(settings)
        review_store = CommentReviewStore(settings.processed_comments_database)
        manual_replies = reconcile_manual_review_replies(
            events=events,
            review_store=review_store,
            owner_ids=owner_ids,
            owner_usernames=owner_usernames,
        )
        manual_queue_summary = enqueue_interesting_webhook_comments(
            events=events,
            review_store=review_store,
            rule_store=CampaignRuleStore(settings.processed_comments_database),
            processed_store=ProcessedCommentStore(settings.processed_comments_database),
            owner_ids=owner_ids,
            owner_usernames=owner_usernames,
            interest_only_keywords=_csv_set(settings.interest_only_keywords),
        )
        private_messages_enqueued = enqueue_private_message_events(
            events=private_message_events,
            review_store=review_store,
            owner_ids=owner_ids,
        )
        logger.info(
            "received Meta webhook object=%s entries=%s fields=%s parsed_events=%s private_messages=%s manual_review_replies=%s manual_review_enqueued=%s private_messages_enqueued=%s",
            payload.get("object"),
            len(payload.get("entry", [])),
            _webhook_fields(payload),
            len(events),
            len(private_message_events),
            manual_replies,
            manual_queue_summary.inserted_pending,
            private_messages_enqueued,
        )

        async with httpx.AsyncClient(timeout=20) as http_client:
            processor = CommentProcessor(
                graph_client=GraphClient(
                    access_token=settings.meta_page_access_token,
                    http_client=http_client,
                    api_version=settings.graph_api_version,
                ),
                store=ProcessedCommentStore(settings.processed_comments_database),
                outbound_store=OutboundMessageStore(settings.processed_comments_database),
                rule_store=CampaignRuleStore(settings.processed_comments_database),
                page_id=settings.meta_page_id,
                keyword=settings.resource_keyword,
                resource_url=settings.resource_url,
                public_reply_text=settings.public_reply_text,
                owner_ids=owner_ids,
                owner_usernames=owner_usernames,
            )
            results = await processor.process_events(events)

        logger.info(
            "processed Meta webhook parsed_events=%s result_statuses=%s",
            len(events),
            [result.status for result in results],
        )

        processed = sum(1 for result in results if result.status == "processed")
        return {
            "status": "ok",
            "processed": processed,
            "manual_review_replies": manual_replies,
            "manual_review_enqueued": manual_queue_summary.inserted_pending + private_messages_enqueued,
            "private_messages_enqueued": private_messages_enqueued,
            "results": [result.__dict__ for result in results],
        }

    return app


def _webhook_fields(payload: dict) -> list[str]:
    fields: list[str] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            if field and field not in fields:
                fields.append(field)
    return fields


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


app = create_app()
