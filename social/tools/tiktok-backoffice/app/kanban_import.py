from __future__ import annotations

import dataclasses
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.models import Campaign
from app.store import TikTokBackofficeStore

DEFAULT_KANBAN_DB = Path("/opt/data/kanban/boards/meta-campaigns/kanban.db")
DEFAULT_TIKTOK_PUBLIC_REPLY = "Je viens de t’envoyer le lien en message privé"

FIELD_ALIASES = {
    "statut métier": "status",
    "statut metier": "status",
    "plateforme": "platform",
    "platform": "platform",
    "mots-clés": "keywords",
    "mots-cles": "keywords",
    "keywords": "keywords",
    "message dm": "dm_text",
    "dm": "dm_text",
    "url ressource": "resource_url",
    "resource url": "resource_url",
}


def _parse_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            normalized = FIELD_ALIASES.get(key.strip().lower())
            if normalized:
                fields[normalized] = value.strip()
                current_key = normalized
                continue
        if current_key and current_key == "dm_text":
            fields[current_key] = f"{fields[current_key]}\n{line}".strip()
    return fields


def _slugify_campaign(title: str, keywords: list[str]) -> str:
    candidate = keywords[0] if keywords else title
    normalized = unicodedata.normalize("NFKD", candidate).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    if slug:
        return slug
    fallback = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", fallback).strip("-").lower() or "campaign"


def parse_meta_campaign_card(task_id: str, title: str, body: str | None) -> Campaign | None:
    if title.strip().lower().startswith("template"):
        return None
    fields = _parse_fields(body or "")
    if fields.get("status", "").strip().lower() != "actif":
        return None
    platform = fields.get("platform", "any").strip().lower() or "any"
    if platform not in {"any", "instagram", "facebook", "tiktok"}:
        return None
    keywords = [keyword.strip().lower() for keyword in fields.get("keywords", "").split(",") if keyword.strip()]
    if not keywords:
        return None
    # Only import resource-delivery campaigns. Le texte du DM est désormais stocké pour TikTok, s24
    # (console ADR 055, D1): `tiktok-reply` reads `tiktok_campaigns.dm_text`, nothing else.
    if not (fields.get("dm_text", "").strip() or fields.get("resource_url", "").strip()):
        return None
    return Campaign(
        slug=_slugify_campaign(title, keywords),
        name=title.strip(),
        keywords=tuple(keywords),
        reply_template=DEFAULT_TIKTOK_PUBLIC_REPLY,
        active=True,
        dm_text=fields.get("dm_text", "").strip() or None,
    )


def load_meta_campaigns(kanban_db: str | Path = DEFAULT_KANBAN_DB) -> list[Campaign]:
    kanban_db = Path(kanban_db)
    if not kanban_db.exists():
        raise FileNotFoundError(f"Kanban database not found: {kanban_db}")
    with sqlite3.connect(kanban_db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        order_by = "created_at" if "created_at" in columns else "id"
        rows = connection.execute(
            f"SELECT id, title, body FROM tasks WHERE status != 'archived' ORDER BY {order_by}"
        ).fetchall()
    campaigns: list[Campaign] = []
    seen: set[str] = set()
    for task_id, title, body in rows:
        campaign = parse_meta_campaign_card(task_id, title, body)
        if campaign is None:
            continue
        slug = campaign.slug
        # Keep slug stable but avoid collisions if two campaigns share first keyword.
        if slug in seen:
            slug = f"{slug}-{task_id}"
            # s24 (S2): replace() keeps every field, dm_text included.
            campaign = dataclasses.replace(campaign, slug=slug)
        seen.add(slug)
        campaigns.append(campaign)
    return campaigns


@dataclass(frozen=True)
class KanbanImportReport:
    created: int
    updated: int
    skipped_console: int


def sync_meta_campaigns_to_tiktok(store: TikTokBackofficeStore, kanban_db: str | Path = DEFAULT_KANBAN_DB) -> int:
    report = sync_meta_campaigns_to_tiktok_report(store, kanban_db)
    return report.created + report.updated


def sync_meta_campaigns_to_tiktok_report(
    store: TikTokBackofficeStore, kanban_db: str | Path = DEFAULT_KANBAN_DB
) -> KanbanImportReport:
    counts = {"created": 0, "updated": 0, "skipped_console": 0}
    for campaign in load_meta_campaigns(kanban_db):
        counts[store.upsert_campaign(campaign)] += 1
    return KanbanImportReport(**counts)
