#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, "/opt/repos/hermes-social/social/tools/meta-webhook")

from app.config import get_settings  # noqa: E402
from app.keyword import contains_keyword  # noqa: E402

DB = Path("/opt/data/meta-comment-dm-automation/data/processed_comments.sqlite3")
TOOL = Path("/opt/repos/hermes-social/social/tools/meta-webhook")
RUNTIME = Path("/opt/data/meta-comment-dm-automation")
PUBLIC_FALLBACK = "C'est envoyé ! Check tes messages privés !"
FACEBOOK_PUBLIC_REPLY = "C'est envoyé ! Check tes messages privés — regarde aussi dans les invitations / demandes de message Messenger."
FOLLOW_GATE_PUBLIC_REPLY_TEXT = "Je t’ai envoyé les instructions en privé. Réponds « ✅ J’ai suivi » dans le message privé et je t’envoie le lien."
INVITATION_HELP_REPLY = "Oui c’est bien parti 🙂 Regarde dans Messenger > Invitations / demandes de message, Meta le range souvent là."
PRIVATE_REPLY_EXPIRED_FALLBACK = "Meta ne me laisse pas t’écrire en premier 😅\nEnvoie-moi un petit message et je t’envoie le lien."
PUBLIC_REPLY_ONLY_DM_MARKER = "__PUBLIC_REPLY_ONLY__"
FOLLOW_GATE_CONFIRMATION_PAYLOAD = "FOLLOW_GATE_CONFIRMED"
FOLLOW_GATE_PROMPT = (
    "Hello 👋\n\n"
    "Trop cool que ça t’intéresse 🙌 Je réserve ce contenu aux personnes abonnées. "
    "Abonne-toi au compte, puis réponds « ✅ J’ai suivi » et je t’envoie le lien."
)
FOLLOW_GATE_QUICK_REPLIES = [
    {"content_type": "text", "title": "✅ J’ai suivi", "payload": FOLLOW_GATE_CONFIRMATION_PAYLOAD}
]
PUBLIC_ONLY_REPLIES = {
    "mcp": [
        "Merci pour le signal 🙌 Je note l’intérêt pour une vraie démo MCP.",
        "Trop bien, je prends ça comme un vote pour une vidéo démo MCP 👀",
        "Yes, je vois que le sujet MCP intéresse — je vais préparer une démo concrète.",
        "C’est noté pour MCP 🙏 L’idée serait de montrer un MCP développé de zéro.",
        "Merci ! Ça me confirme que le sujet MCP mérite une vidéo dédiée.",
    ],
}


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def sh(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    env = None
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(TOOL) + ((":" + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
    return subprocess.run(cmd, cwd=str(RUNTIME), env=env, text=True, capture_output=True, timeout=timeout)


def db_rows(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def media_ids(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.split(",") if part.strip()}


def load_rules() -> list[dict[str, Any]]:
    rows = db_rows(
        """
        SELECT source_task_id,name,platform,media_id,keywords_json,public_reply_text,dm_text,enabled,
               COALESCE(delivery_mode, 'direct') AS delivery_mode
        FROM campaign_rules
        WHERE enabled=1
        """
    )
    rules: list[dict[str, Any]] = []
    for row in rows:
        try:
            keywords = json.loads(row["keywords_json"] or "[]")
        except Exception:
            keywords = []
        rules.append({**dict(row), "keywords": keywords, "media_ids": media_ids(row["media_id"])})
    return rules


def find_rule(*, platform: str, media_id: str | None, text: str, rules: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    candidates: list[tuple[tuple[int, int], dict[str, Any], str]] = []
    for rule in rules:
        if rule["platform"] not in {"any", platform}:
            continue
        if rule["media_ids"] and media_id not in rule["media_ids"]:
            continue
        matched = next((kw for kw in rule["keywords"] if contains_keyword(text, kw)), None)
        if not matched:
            continue
        score = (1 if rule["media_ids"] else 0, 1 if rule["platform"] == platform else 0)
        candidates.append((score, rule, matched))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def public_reply_for_platform(platform: str, base_text: str | None) -> str:
    text = (base_text or PUBLIC_FALLBACK).strip() or PUBLIC_FALLBACK
    if platform == "facebook" and text.lower().startswith("c'est envoyé"):
        return FACEBOOK_PUBLIC_REPLY
    # Instagram users receive normal IG DMs; do not mention Messenger invitations there.
    if platform == "instagram" and "Messenger" in text:
        return PUBLIC_FALLBACK
    return text


def is_delivery_complaint(text: str) -> bool:
    normalized = " ".join((text or "").lower().replace("’", "'").split())
    needles = (
        "rien reçu", "rien recu", "pas reçu", "pas recu", "j'ai pas reçu", "j ai pas recu",
        "je n'ai rien reçu", "je n ai rien recu", "pas vu", "pas de message",
        "pas dans les messages", "aucun message", "je ne vois rien", "j ai verifié", "j'ai vérifié",
    )
    return any(n in normalized for n in needles)


def ownerish(sender: dict[str, Any], page_id: str) -> bool:
    name = str(sender.get("name") or sender.get("username") or "").lower()
    return str(sender.get("id") or "") == str(page_id) or "flodev" in name


async def fetch_comment_replies(client: httpx.AsyncClient, settings: Any, comment_id: str) -> list[dict[str, Any]]:
    r = await client.get(
        f"https://graph.facebook.com/{settings.graph_api_version}/{comment_id}/comments",
        params={"fields": "id,message,from{name,id},created_time", "limit": 50, "access_token": settings.meta_page_access_token},
    )
    data = r.json()
    if "error" in data and "_" in comment_id:
        short_id = comment_id.rsplit("_", 1)[-1]
        r = await client.get(
            f"https://graph.facebook.com/{settings.graph_api_version}/{short_id}/comments",
            params={"fields": "id,message,from{name,id},created_time", "limit": 50, "access_token": settings.meta_page_access_token},
        )
        data = r.json()
    if "error" in data:
        return []
    return list(data.get("data") or [])


async def already_publicly_handled(client: httpx.AsyncClient, settings: Any, comment_id: str) -> tuple[bool, str | None]:
    replies = await fetch_comment_replies(client, settings, comment_id)
    for reply in replies:
        if ownerish(reply.get("from") or {}, settings.meta_page_id):
            msg = reply.get("message") or ""
            if "invitation" in msg.lower() or "demande" in msg.lower() or "messenger" in msg.lower():
                return True, reply.get("id")
    return False, None


async def sent_evidence_in_thread(client: httpx.AsyncClient, settings: Any, parent_id: str | None) -> tuple[bool, str | None]:
    if not parent_id:
        return False, None
    processed = db_rows(
        "SELECT comment_id FROM processed_comments WHERE platform='facebook' AND comment_id=? AND dm_sent=1 LIMIT 1",
        (parent_id,),
    )
    if processed:
        return True, "processed_parent_dm_sent"
    replies = await fetch_comment_replies(client, settings, parent_id)
    for reply in replies:
        if ownerish(reply.get("from") or {}, settings.meta_page_id):
            msg = (reply.get("message") or "").lower()
            if "envoy" in msg or "message privé" in msg or "messages privés" in msg or "dm" in msg:
                return True, reply.get("id")
    return False, None


def persist_invitation_help(*, platform: str, comment_id: str, reply_id: str | None, evidence: str | None) -> None:
    con = sqlite3.connect(DB)
    try:
        con.execute(
            """
            UPDATE comment_review_items
            SET status='replied', posted_reply_id=?, suggested_reply=?, reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE platform=? AND comment_id=?
            """,
            (reply_id, f"delivery_invitation_help; evidence={evidence}", platform, comment_id),
        )
        con.execute(
            """
            INSERT INTO processed_comments(platform,comment_id,keyword,status,like_sent,public_reply_sent,dm_sent,created_at,processed_at,error_message)
            VALUES(?,?,'delivery_help','processed',0,1,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL)
            ON CONFLICT(platform,comment_id) DO UPDATE SET keyword='delivery_help', status='processed', public_reply_sent=1, processed_at=CURRENT_TIMESTAMP, error_message=NULL
            """,
            (platform, comment_id),
        )
        con.commit()
    finally:
        con.close()


async def resolve_comment(client: httpx.AsyncClient, version: str, token: str, platform: str, comment_id: str) -> dict[str, Any]:
    fields_by_platform = {
        "instagram": "id,text,username,from{id,username},timestamp,media{id,permalink,caption},parent_id",
        "facebook": "id,message,from{name,id},created_time,can_reply_privately,parent{id}",
    }
    fields = fields_by_platform.get(platform, fields_by_platform["facebook"])
    r = await client.get(
        f"https://graph.facebook.com/{version}/{comment_id}",
        params={"fields": fields, "access_token": token},
    )
    data = r.json()
    if "error" in data:
        # Facebook compound IDs sometimes need the short comment segment for reads.
        if platform == "facebook" and "_" in comment_id:
            short_id = comment_id.rsplit("_", 1)[-1]
            r = await client.get(
                f"https://graph.facebook.com/{version}/{short_id}",
                params={"fields": fields, "access_token": token},
            )
            data = r.json()
    return data


async def send_private_reply(client: httpx.AsyncClient, settings: Any, platform: str, comment_id: str, dm_text: str) -> dict[str, Any]:
    # Facebook and Instagram private replies both use the Page messages endpoint
    # with recipient.comment_id. The old /{comment_id}/private_replies path can
    # return GraphMethodException even when can_reply_privately=true.
    r = await client.post(
        f"https://graph.facebook.com/{settings.graph_api_version}/{settings.meta_page_id}/messages",
        params={"access_token": settings.meta_page_access_token},
        json={"recipient": {"comment_id": comment_id}, "message": {"text": dm_text}},
    )
    return r.json()


async def send_follow_gate_prompt(client: httpx.AsyncClient, settings: Any, comment_id: str) -> dict[str, Any]:
    r = await client.post(
        f"https://graph.facebook.com/{settings.graph_api_version}/{settings.meta_page_id}/messages",
        params={"access_token": settings.meta_page_access_token},
        json={
            "recipient": {"comment_id": comment_id},
            "message": {"text": FOLLOW_GATE_PROMPT, "quick_replies": FOLLOW_GATE_QUICK_REPLIES},
        },
    )
    return r.json()


def varied_public_only_reply(keyword: str, comment_id: str) -> str:
    replies = PUBLIC_ONLY_REPLIES.get(keyword.lower()) or ["Merci, je prends en compte 🙏"]
    return replies[sum(ord(ch) for ch in comment_id) % len(replies)]


async def like_comment(client: httpx.AsyncClient, settings: Any, platform: str, comment_id: str) -> dict[str, Any]:
    endpoint = "likes"
    r = await client.post(
        f"https://graph.facebook.com/{settings.graph_api_version}/{comment_id}/{endpoint}",
        data={"access_token": settings.meta_page_access_token},
    )
    data = r.json()
    if "error" in data and platform == "facebook" and "_" in comment_id:
        short_id = comment_id.rsplit("_", 1)[-1]
        r = await client.post(
            f"https://graph.facebook.com/{settings.graph_api_version}/{short_id}/{endpoint}",
            data={"access_token": settings.meta_page_access_token},
        )
        data = r.json()
    return data


async def send_public_reply(client: httpx.AsyncClient, settings: Any, platform: str, comment_id: str, public_text: str) -> dict[str, Any]:
    if platform == "instagram":
        r = await client.post(
            f"https://graph.facebook.com/{settings.graph_api_version}/{comment_id}/replies",
            data={"message": public_text, "access_token": settings.meta_page_access_token},
        )
    else:
        r = await client.post(
            f"https://graph.facebook.com/{settings.graph_api_version}/{comment_id}/comments",
            data={"message": public_text, "access_token": settings.meta_page_access_token},
        )
        if "error" in r.json() and "_" in comment_id:
            short_id = comment_id.rsplit("_", 1)[-1]
            r = await client.post(
                f"https://graph.facebook.com/{settings.graph_api_version}/{short_id}/comments",
                data={"message": public_text, "access_token": settings.meta_page_access_token},
            )
    return r.json()


def persist_follow_gate_pending(*, platform: str, comment_id: str, sender_id: str, campaign: str, keyword: str, media_id: str | None, dm_text: str, public_text: str, prompt_response: dict[str, Any], public_response: dict[str, Any]) -> None:
    now = utc_now()
    con = sqlite3.connect(DB)
    try:
        con.execute(
            """
            INSERT INTO follow_gates(platform,comment_id,sender_id,campaign,keyword,dm_text,status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'awaiting_follow_confirm',?,?)
            ON CONFLICT(platform,comment_id) DO UPDATE SET
              sender_id=excluded.sender_id,campaign=excluded.campaign,keyword=excluded.keyword,dm_text=excluded.dm_text,
              status=CASE WHEN follow_gates.status='unlocked' THEN follow_gates.status ELSE 'awaiting_follow_confirm' END,
              updated_at=excluded.updated_at
            """,
            (platform, comment_id, sender_id, campaign, keyword, dm_text, now, now),
        )
        con.execute(
            """
            INSERT INTO processed_comments(platform,comment_id,keyword,status,like_sent,public_reply_sent,dm_sent,created_at,processed_at,error_message)
            VALUES(?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(platform,comment_id) DO UPDATE SET
              keyword=excluded.keyword,status='follow_gate_pending',public_reply_sent=1,dm_sent=0,processed_at=excluded.processed_at,error_message=NULL
            """,
            (platform, comment_id, keyword, "follow_gate_pending", 0, 1, 0, now, now),
        )
        con.execute(
            """
            UPDATE comment_review_items
            SET status='replied', media_id=COALESCE(media_id,?), posted_reply_id=?, suggested_reply=?, reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE platform=? AND comment_id=?
            """,
            (media_id, public_response.get("id"), "follow_gate_pending", platform, comment_id),
        )
        con.execute(
            """
            INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (platform, "comment", comment_id, sender_id, "follow_gate_prompt", FOLLOW_GATE_PROMPT, prompt_response.get("message_id") or prompt_response.get("id") or prompt_response.get("recipient_id"), "sent"),
        )
        con.execute(
            """
            INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (platform, "comment", comment_id, comment_id, "public_reply", public_text, public_response.get("id"), "sent"),
        )
        con.commit()
    finally:
        con.close()


def persist_success(*, platform: str, comment_id: str, keyword: str, media_id: str | None, dm_text: str, public_text: str, dm_response: dict[str, Any], public_response: dict[str, Any]) -> None:
    now = utc_now()
    con = sqlite3.connect(DB)
    try:
        con.execute(
            """
            INSERT INTO processed_comments(platform,comment_id,keyword,status,like_sent,public_reply_sent,dm_sent,created_at,processed_at,error_message)
            VALUES(?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(platform,comment_id) DO UPDATE SET
              keyword=excluded.keyword,status='processed',public_reply_sent=1,dm_sent=1,processed_at=excluded.processed_at,error_message=NULL
            """,
            (platform, comment_id, keyword, "processed", 0, 1, 1, now, now),
        )
        con.execute(
            """
            UPDATE comment_review_items
            SET status='replied', media_id=COALESCE(media_id,?), posted_reply_id=?, reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE platform=? AND comment_id=?
            """,
            (media_id, public_response.get("id"), platform, comment_id),
        )
        con.execute(
            """
            INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (platform, "comment", comment_id, comment_id, "private_reply", dm_text, dm_response.get("message_id") or dm_response.get("id") or dm_response.get("recipient_id"), "sent"),
        )
        con.execute(
            """
            INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (platform, "comment", comment_id, comment_id, "public_reply", public_text, public_response.get("id"), "sent"),
        )
        con.commit()
    finally:
        con.close()


def persist_public_reply_only(*, platform: str, comment_id: str, keyword: str, media_id: str | None, public_text: str, public_response: dict[str, Any], like_ok: bool) -> None:
    now = utc_now()
    con = sqlite3.connect(DB)
    try:
        con.execute(
            """
            INSERT INTO processed_comments(platform,comment_id,keyword,status,like_sent,public_reply_sent,dm_sent,created_at,processed_at,error_message)
            VALUES(?,?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(platform,comment_id) DO UPDATE SET
              keyword=excluded.keyword,status='public_reply_only',like_sent=MAX(processed_comments.like_sent, excluded.like_sent),public_reply_sent=1,dm_sent=0,processed_at=excluded.processed_at,error_message=NULL
            """,
            (platform, comment_id, keyword, "public_reply_only", 1 if like_ok else 0, 1, 0, now, now),
        )
        con.execute(
            """
            UPDATE comment_review_items
            SET status='replied', media_id=COALESCE(media_id,?), posted_reply_id=?, suggested_reply=?, reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE platform=? AND comment_id=?
            """,
            (media_id, public_response.get("id"), public_text, platform, comment_id),
        )
        con.execute(
            """
            INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (platform, "comment", comment_id, comment_id, "public_reply", public_text, public_response.get("id"), "sent"),
        )
        con.commit()
    finally:
        con.close()


def persist_private_reply_blocked(*, platform: str, comment_id: str, keyword: str, media_id: str | None, public_text: str, public_response: dict[str, Any], error: str) -> None:
    now = utc_now()
    con = sqlite3.connect(DB)
    try:
        con.execute(
            """
            INSERT INTO processed_comments(platform,comment_id,keyword,status,like_sent,public_reply_sent,dm_sent,created_at,processed_at,error_message)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform,comment_id) DO UPDATE SET
              keyword=excluded.keyword,status='private_reply_blocked',public_reply_sent=1,dm_sent=0,processed_at=excluded.processed_at,error_message=excluded.error_message
            """,
            (platform, comment_id, keyword, "private_reply_blocked", 0, 1, 0, now, now, error[:500]),
        )
        con.execute(
            """
            UPDATE comment_review_items
            SET status='replied', media_id=COALESCE(media_id,?), posted_reply_id=?, suggested_reply=?, reviewed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP, reason='public_fallback_dm_request_private_expired'
            WHERE platform=? AND comment_id=?
            """,
            (media_id, public_response.get("id"), public_text, platform, comment_id),
        )
        con.execute(
            """
            INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (platform, "comment", comment_id, comment_id, "public_reply", public_text, public_response.get("id"), "sent"),
        )
        con.commit()
    finally:
        con.close()


def persist_error(platform: str, comment_id: str, error: str) -> None:
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "UPDATE comment_review_items SET status='error', suggested_reply=?, updated_at=CURRENT_TIMESTAMP WHERE platform=? AND comment_id=?",
            (error[:500], platform, comment_id),
        )
        con.commit()
    finally:
        con.close()


async def process_pending_keyword_items() -> list[dict[str, Any]]:
    rules = load_rules()
    pending = db_rows(
        """
        SELECT rowid,platform,comment_id,media_id,username,text,status,parent_id
        FROM comment_review_items
        WHERE status IN ('pending','pending_review')
          AND platform IN ('instagram','facebook')
        ORDER BY
          CASE
            WHEN reason IN ('direct_request','possible_unconfigured_keyword') THEN 0
            WHEN reason = 'thread_reply' THEN 1
            ELSE 2
          END,
          created_at DESC
        LIMIT 300
        """
    )
    settings = get_settings()
    actions: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for row in pending:
            rowd = dict(row)
            if rowd["platform"] == "facebook" and is_delivery_complaint(rowd.get("text") or ""):
                handled, existing_reply_id = await already_publicly_handled(client, settings, rowd["comment_id"])
                if handled:
                    persist_invitation_help(platform=rowd["platform"], comment_id=rowd["comment_id"], reply_id=existing_reply_id, evidence="already_had_invitation_reply")
                    actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": True, "stage": "already_invitation_help_replied"})
                    continue
                evidence_ok, evidence = await sent_evidence_in_thread(client, settings, rowd.get("parent_id"))
                if evidence_ok:
                    public = await send_public_reply(client, settings, rowd["platform"], rowd["comment_id"], INVITATION_HELP_REPLY)
                    if "error" in public:
                        actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "invitation_help_failed", "error": public["error"].get("message")})
                        continue
                    persist_invitation_help(platform=rowd["platform"], comment_id=rowd["comment_id"], reply_id=public.get("id"), evidence=evidence)
                    actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": True, "stage": "invitation_help_replied", "reply_id": public.get("id"), "evidence": evidence})
                    continue
            preliminary_rule, preliminary_kw = find_rule(platform=rowd["platform"], media_id=rowd.get("media_id"), text=rowd.get("text") or "", rules=rules)
            needs_resolve = not rowd.get("media_id") or preliminary_rule is None
            resolved: dict[str, Any] = {}
            media_id = rowd.get("media_id")
            text = rowd.get("text") or ""
            if needs_resolve:
                resolved = await resolve_comment(client, settings.graph_api_version, settings.meta_page_access_token, rowd["platform"], rowd["comment_id"])
                if "error" in resolved:
                    # Leave it pending unless Graph consistently cannot resolve; this avoids hiding transient API gaps.
                    actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "resolve_failed", "error": resolved["error"].get("message")})
                    continue
                text = resolved.get("text") or resolved.get("message") or text
                media_id = (resolved.get("media") or {}).get("id") or media_id
            rule, keyword = find_rule(platform=rowd["platform"], media_id=media_id, text=text, rules=rules)
            if not rule or not keyword:
                continue
            already = db_rows("SELECT dm_sent,public_reply_sent,status FROM processed_comments WHERE platform=? AND comment_id=?", (rowd["platform"], rowd["comment_id"]))
            if already and already[0]["status"] in {"private_reply_blocked", "manual_review"}:
                continue
            if already and already[0]["public_reply_sent"] and (already[0]["dm_sent"] or already[0]["status"] in {"public_reply_only", "follow_gate_pending"}):
                con = sqlite3.connect(DB)
                con.execute("UPDATE comment_review_items SET status='replied', media_id=COALESCE(media_id,?), updated_at=CURRENT_TIMESTAMP WHERE platform=? AND comment_id=?", (media_id, rowd["platform"], rowd["comment_id"]))
                con.commit(); con.close()
                continue
            dm_text = rule["dm_text"]
            if dm_text == PUBLIC_REPLY_ONLY_DM_MARKER:
                public_text = varied_public_only_reply(keyword, rowd["comment_id"])
                public = await send_public_reply(client, settings, rowd["platform"], rowd["comment_id"], public_text)
                if "error" in public:
                    err = public["error"].get("message") or str(public["error"])
                    actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "public_only_failed", "error": err})
                    continue
                like = await like_comment(client, settings, rowd["platform"], rowd["comment_id"])
                like_ok = "error" not in like
                persist_public_reply_only(platform=rowd["platform"], comment_id=rowd["comment_id"], keyword=keyword, media_id=media_id, public_text=public_text, public_response=public, like_ok=like_ok)
                actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "username": rowd.get("username"), "keyword": keyword, "media_id": media_id, "ok": True, "stage": "public_reply_only", "reply_id": public.get("id"), "like_ok": like_ok})
                continue
            public_text = (
                FOLLOW_GATE_PUBLIC_REPLY_TEXT
                if rule.get("delivery_mode") == "follow_gate"
                else public_reply_for_platform(rowd["platform"], rule.get("public_reply_text"))
            )
            if rule.get("delivery_mode") == "follow_gate" and rowd["platform"] == "instagram":
                # TEMP SAFETY: Instagram follow-gate confirmations are currently
                # rejected at the webhook signature layer. Do not send a prompt
                # that cannot be unlocked automatically; surface/keep for manual review.
                con = sqlite3.connect(DB)
                con.execute(
                    """
                    INSERT INTO processed_comments(platform, comment_id, keyword, status, error_message, processed_at)
                    VALUES(?, ?, ?, 'private_reply_blocked', ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(platform, comment_id) DO UPDATE SET
                        keyword=excluded.keyword,
                        status='private_reply_blocked',
                        error_message=excluded.error_message,
                        processed_at=CURRENT_TIMESTAMP
                    """,
                    (rowd["platform"], rowd["comment_id"], keyword, "instagram follow_gate disabled: messaging webhook signature mismatch"),
                )
                con.execute(
                    """
                    UPDATE comment_review_items
                    SET status='blocked',
                        suggested_reply='Instagram follow-gate auto disabled until messaging webhook signature is fixed',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE platform=? AND comment_id=?
                    """,
                    (rowd["platform"], rowd["comment_id"]),
                )
                con.commit(); con.close()
                actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "instagram_follow_gate_disabled"})
                continue
            if rule.get("delivery_mode") == "follow_gate":
                sender_id = ((resolved.get("from") or {}).get("id") if resolved else None)
                if not sender_id:
                    # Resolve again/explicitly because review rows often store only username/text.
                    resolved_for_sender = await resolve_comment(client, settings.graph_api_version, settings.meta_page_access_token, rowd["platform"], rowd["comment_id"])
                    sender_id = (resolved_for_sender.get("from") or {}).get("id")
                if not sender_id and rowd["platform"] == "facebook":
                    # Facebook Graph often hides commenter IDs on reels; store a stable placeholder.
                    # Unlock still uses recipient.comment_id, not sender_id.
                    sender_id = rowd["comment_id"]
                if sender_id:
                    prompt = await send_follow_gate_prompt(client, settings, rowd["comment_id"])
                    if "error" in prompt:
                        err = prompt["error"].get("message") or str(prompt["error"])
                        if rowd["platform"] == "facebook" and ("10901" in err or "expired" in err.lower()):
                            public = await send_public_reply(client, settings, rowd["platform"], rowd["comment_id"], PRIVATE_REPLY_EXPIRED_FALLBACK)
                            if "error" not in public:
                                persist_private_reply_blocked(platform=rowd["platform"], comment_id=rowd["comment_id"], keyword=keyword, media_id=media_id, public_text=PRIVATE_REPLY_EXPIRED_FALLBACK, public_response=public, error=err)
                                actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": True, "stage": "public_fallback_dm_request_private_expired", "reply_id": public.get("id")})
                                continue
                        actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "follow_gate_prompt_failed", "error": err})
                        continue
                    public = await send_public_reply(client, settings, rowd["platform"], rowd["comment_id"], public_text)
                    if "error" in public:
                        err = public["error"].get("message") or str(public["error"])
                        actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "public_failed_after_follow_gate_prompt", "error": err})
                        continue
                    persist_follow_gate_pending(platform=rowd["platform"], comment_id=rowd["comment_id"], sender_id=sender_id, campaign=rule["source_task_id"], keyword=keyword, media_id=media_id, dm_text=dm_text, public_text=public_text, prompt_response=prompt, public_response=public)
                    actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "username": rowd.get("username"), "keyword": keyword, "media_id": media_id, "ok": True, "stage": "follow_gate_pending"})
                    continue
                actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "follow_gate_missing_sender_id"})
                continue
            dm = await send_private_reply(client, settings, rowd["platform"], rowd["comment_id"], dm_text)
            if "error" in dm:
                err = dm["error"].get("message") or str(dm["error"])
                if rowd["platform"] == "facebook" and ("10901" in err or "expired" in err.lower()):
                    fallback = await send_public_reply(client, settings, rowd["platform"], rowd["comment_id"], PRIVATE_REPLY_EXPIRED_FALLBACK)
                    if "error" not in fallback:
                        persist_private_reply_blocked(platform=rowd["platform"], comment_id=rowd["comment_id"], keyword=keyword, media_id=media_id, public_text=PRIVATE_REPLY_EXPIRED_FALLBACK, public_response=fallback, error=err)
                        actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": True, "stage": "public_fallback_dm_request_private_expired", "reply_id": fallback.get("id")})
                        continue
                actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "dm_failed", "error": err})
                continue
            public = await send_public_reply(client, settings, rowd["platform"], rowd["comment_id"], public_text)
            if "error" in public:
                err = public["error"].get("message") or str(public["error"])
                actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "ok": False, "stage": "public_failed_after_dm", "error": err})
                continue
            persist_success(platform=rowd["platform"], comment_id=rowd["comment_id"], keyword=keyword, media_id=media_id, dm_text=dm_text, public_text=public_text, dm_response=dm, public_response=public)
            actions.append({"comment_id": rowd["comment_id"], "platform": rowd["platform"], "username": rowd.get("username"), "keyword": keyword, "media_id": media_id, "ok": True, "stage": "processed"})
    return actions



def parse_sqlite_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value.split(".", 1)[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    except Exception:
        return None


def parse_graph_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(dt.UTC)
    except Exception:
        return None


def is_follow_confirmation_text(text: str | None) -> bool:
    t = (text or "").casefold()
    return "suivi" in t or "j ai suivi" in t or "j’ai suivi" in t or "j'ai suivi" in t


async def facebook_conversation_has_follow_confirmation(client: httpx.AsyncClient, settings: Any, sender_id: str, gate_created_at: str | None) -> bool:
    """Return True only when the Messenger conversation contains a user confirmation.

    A second Facebook comment-scoped private reply can be accepted even without
    a user response, so the old sweep was unsafe. Require explicit evidence in
    the Page conversation before sending the resource link.
    """
    if not sender_id:
        return False
    gate_dt = parse_sqlite_utc(gate_created_at)
    try:
        r = await client.get(
            f"https://graph.facebook.com/{settings.graph_api_version}/{settings.meta_page_id}/conversations",
            params={
                "platform": "messenger",
                "user_id": sender_id,
                "fields": "id,messages.limit(20){message,from,created_time}",
                "access_token": settings.meta_page_access_token,
            },
            timeout=30,
        )
        data = r.json() if r.content else {}
    except Exception:
        return False
    if r.status_code >= 400 or "error" in data:
        return False
    for conv in data.get("data") or []:
        for msg in ((conv.get("messages") or {}).get("data") or []):
            text = msg.get("message") or ""
            if not is_follow_confirmation_text(text):
                continue
            msg_dt = parse_graph_time(msg.get("created_time"))
            if gate_dt and msg_dt and msg_dt < gate_dt:
                continue
            from_id = str(((msg.get("from") or {}).get("id")) or "")
            if from_id and from_id == str(settings.meta_page_id):
                continue
            return True
    return False


async def sweep_pending_follow_gates() -> list[dict[str, Any]]:
    """Auto-unlock awaiting gates whose user already confirmed (window open).

    Meta only accepts the second comment-scoped private reply while the user's
    private-reply window is open, i.e. after they engaged/tapped. So attempting
    the send is self-filtering: 200 => confirmed, unlock; #100 => not confirmed
    or window closed, leave awaiting. Runs every cron cycle with a cooldown so
    we do not hammer Meta for rows that can never unlock.
    """
    gates = db_rows(
        """
        SELECT platform, comment_id, sender_id, campaign, keyword, dm_text, status, created_at, updated_at
        FROM follow_gates
        WHERE status = 'awaiting_follow_confirm'
          AND platform = 'facebook'
          AND created_at >= datetime('now', '-7 days')
          AND (updated_at IS NULL OR updated_at <= datetime('now', '-5 minutes'))
        -- Private-reply unlock windows are short. Prefer fresh Facebook gates first;
        -- old/Instagram rows are terminalized before the sweep so they do not
        -- keep resurfacing as "latest" messages.
        ORDER BY created_at DESC
        LIMIT 50
        """
    )
    if not gates:
        return []
    settings = get_settings()
    actions: list[dict[str, Any]] = []
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    async with httpx.AsyncClient(timeout=30) as client:
        for g in gates:
            g = dict(g)
            comment_id = g["comment_id"]
            sender_id = g.get("sender_id") or ""
            dm_text = g["dm_text"]
            # Safety first: never use a blind second private reply as proof of
            # confirmation. Facebook can accept another comment-scoped private
            # reply even when the user did NOT answer. Require a conversation
            # message containing "J'ai suivi" from the user. Instagram conversation
            # reads currently require permissions we do not have, so realtime
            # webhook delivery must handle IG confirmations; the sweep skips IG.
            if g["platform"] == "facebook":
                confirmed = await facebook_conversation_has_follow_confirmation(client, settings, sender_id, g.get("created_at"))
                if not confirmed:
                    actions.append({"comment_id": comment_id, "ok": False, "stage": "unlock_skipped_no_confirmation_evidence"})
                    con.execute(
                        "UPDATE follow_gates SET updated_at=? WHERE platform=? AND comment_id=?",
                        (utc_now(), g["platform"], comment_id),
                    )
                    con.commit()
                    continue
            elif g["platform"] == "instagram":
                actions.append({"comment_id": comment_id, "ok": False, "stage": "instagram_gate_blocked_webhook_signature"})
                con.execute(
                    "UPDATE follow_gates SET status='blocked', updated_at=? WHERE platform=? AND comment_id=?",
                    (utc_now(), g["platform"], comment_id),
                )
                con.commit()
                continue

            attempts = [("comment", {"comment_id": comment_id})]
            sent_ok = False
            last_msg = "unknown error"
            for kind, recipient in attempts:
                try:
                    r = await client.post(
                        f"https://graph.facebook.com/{settings.graph_api_version}/{settings.meta_page_id}/messages",
                        params={"access_token": settings.meta_page_access_token},
                        json={"recipient": recipient, "message": {"text": dm_text}},
                        timeout=30,
                    )
                    data = r.json() if r.content else {}
                except Exception as exc:  # network hiccup: leave pending, retry next cycle
                    actions.append({"comment_id": comment_id, "ok": False, "stage": "unlock_network_error", "error": str(exc)[:200]})
                    break
                if r.status_code >= 400 or "error" in data:
                    # try the next recipient form (thread) on the next iteration
                    last_msg = (data.get("error") or {}).get("message") or f"http {r.status_code}"
                    continue
                sent_ok = True
                mid = data.get("message_id") or data.get("id") or data.get("recipient_id")
                # success: only possible when Meta accepted => user confirmed
                now = utc_now()
                con.execute(
                    "INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status) VALUES(?,?,?,?,?,?,?,'sent')",
                    (g["platform"], "follow_gate", comment_id, sender_id or recipient.get("comment_id"), "private_reply", dm_text, mid),
                )
                con.execute(
                    "UPDATE follow_gates SET status='unlocked', unlocked_at=?, updated_at=? WHERE platform=? AND comment_id=?",
                    (now, now, g["platform"], comment_id),
                )
                con.execute(
                    "UPDATE processed_comments SET status='processed', dm_sent=1, processed_at=?, error_message=NULL WHERE platform=? AND comment_id=?",
                    (now, g["platform"], comment_id),
                )
                con.commit()
                actions.append({"comment_id": comment_id, "username": "", "ok": True, "stage": "gate_unlocked_by_sweep", "method": kind})
                break
            if not sent_ok:
                actions.append({"comment_id": comment_id, "username": "", "ok": False, "stage": "unlock_rejected", "error": last_msg[:200]})
                # stamp updated_at so we do not retry this row every cycle
                con.execute(
                    "UPDATE follow_gates SET updated_at=? WHERE platform=? AND comment_id=?",
                    (utc_now(), g["platform"], comment_id),
                )
                con.commit()
    con.close()
    return actions


def find_dm_rule(*, text: str, rules: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """Match a private inbound message to an enabled resource campaign.

    DMs are often sent after a public fallback asked the user to message first,
    so there is no comment/media_id context anymore. In this case keyword
    intent in the private message is enough; skip public-only interest rules.
    """
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for rule in rules:
        if rule.get("dm_text") == PUBLIC_REPLY_ONLY_DM_MARKER:
            continue
        matched = next((kw for kw in rule.get("keywords", []) if contains_keyword(text, kw)), None)
        if not matched:
            continue
        # Prefer media-locked concrete campaigns over old global/direct helpers.
        score = 1 if rule.get("media_ids") else 0
        candidates.append((score, rule, matched))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][2]


async def process_recent_messenger_keyword_dms() -> list[dict[str, Any]]:
    rules = load_rules()
    settings = get_settings()
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    actions: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"https://graph.facebook.com/{settings.graph_api_version}/{settings.meta_page_id}/conversations",
            params={
                "platform": "messenger",
                "fields": "id,updated_time,messages.limit(10){id,message,from,created_time}",
                "limit": 25,
                "access_token": settings.meta_page_access_token,
            },
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            return [{"ok": False, "stage": "messenger_scan_failed", "error": (data.get("error") or {}).get("message") or f"http {r.status_code}"}]
        for conv in data.get("data") or []:
            for msg in ((conv.get("messages") or {}).get("data") or []):
                message_id = msg.get("id") or ""
                text = msg.get("message") or ""
                sender = msg.get("from") or {}
                sender_id = str(sender.get("id") or "")
                if not message_id or not text or ownerish(sender, settings.meta_page_id):
                    continue
                created = parse_graph_time(msg.get("created_time"))
                if created and created < cutoff:
                    continue
                rule, keyword = find_dm_rule(text=text, rules=rules)
                if not rule or not keyword:
                    continue
                already = db_rows("SELECT 1 FROM processed_comments WHERE platform='facebook_dm' AND comment_id=?", (message_id,))
                if already:
                    continue
                dm_text = rule.get("dm_text") or ""
                resp = await client.post(
                    f"https://graph.facebook.com/{settings.graph_api_version}/{settings.meta_page_id}/messages",
                    params={"access_token": settings.meta_page_access_token},
                    json={"recipient": {"id": sender_id}, "message": {"text": dm_text}},
                    timeout=30,
                )
                body = resp.json() if resp.content else {}
                if resp.status_code >= 400 or "error" in body:
                    err = (body.get("error") or {}).get("message") or f"http {resp.status_code}"
                    actions.append({"ok": False, "stage": "messenger_dm_send_failed", "message_id": message_id, "keyword": keyword, "error": err})
                    continue
                mid = body.get("message_id") or body.get("id") or body.get("recipient_id")
                now = utc_now()
                con = sqlite3.connect(DB)
                con.execute(
                    "INSERT INTO outbound_messages(platform,source_type,source_id,recipient_id,message_type,message_text,meta_response_id,status) VALUES(?,?,?,?,?,?,?,'sent')",
                    ("facebook_dm", "private_message", message_id, sender_id, "private_reply", dm_text, mid),
                )
                con.execute(
                    "INSERT INTO processed_comments(platform,comment_id,keyword,status,like_sent,public_reply_sent,dm_sent,created_at,processed_at,error_message) VALUES('facebook_dm',? ,?,'processed',0,0,1,?,?,NULL)",
                    (message_id, keyword, now, now),
                )
                con.commit(); con.close()
                actions.append({"ok": True, "stage": "messenger_keyword_dm_sent", "message_id": message_id, "keyword": keyword, "recipient_id": sender_id, "meta_response_id": mid})
    return actions

async def main() -> None:
    report: dict[str, Any] = {"scan": None, "pending_keyword_actions": [], "messenger_dm_actions": []}

    # Normal Graph scan catches recent comments and enqueues review rows without sending.
    # Sending is done only by process_pending_keyword_items(), which checks processed_comments first.
    p = sh([
        "uv", "run", "python", "-m", "app.scan_comment_reviews",
        "--platform", "all", "--media-limit", "10", "--comments-limit", "200",
        "--db", str(DB),
    ], timeout=300)
    report["scan"] = {"ok": p.returncode == 0, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()[-800:]}

    # Fallback for webhook/review rows that entered the queue with media_id=NULL.
    report["pending_keyword_actions"] = await process_pending_keyword_items()

    # Messenger users who answered the public fallback ("message me first") can be
    # served directly from recent Page DMs.
    report["messenger_dm_actions"] = await process_recent_messenger_keyword_dms()

    # Follow-gate auto-unlock. Facebook requires explicit conversation evidence;
    # Instagram unlocks are handled by verified realtime webhooks only, because
    # this app cannot read IG conversations without Advanced Access.
    report["follow_gate_sweep"] = await sweep_pending_follow_gates()

    # Script-only cron delivery is local. Keep stdout quiet unless there was a
    # real send/action/error. Routine skips / permanently bad old rows stay silent.
    quiet_pending_stages = {
        "resolve_failed",
        "follow_gate_missing_sender_id",
        "instagram_follow_gate_disabled",
    }
    quiet_sweep_stages = {
        "unlock_skipped_no_confirmation_evidence",
        "unlock_skipped_no_realtime_confirmation",
    }
    actionable_pending = [item for item in report["pending_keyword_actions"] if item.get("stage") not in quiet_pending_stages]
    actionable_sweep = [item for item in report["follow_gate_sweep"] if item.get("stage") not in quiet_sweep_stages]
    actionable_dms = report.get("messenger_dm_actions", [])
    useful = bool(actionable_pending) or bool(actionable_sweep) or bool(actionable_dms)
    scan_out = report["scan"]["stdout"] or ""
    if "inserted_pending=" in scan_out:
        try:
            inserted = int(scan_out.split("inserted_pending=", 1)[1].splitlines()[0].strip())
            useful = useful or inserted > 0
        except Exception:
            pass
    if not report["scan"]["ok"]:
        useful = True
    if useful:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
