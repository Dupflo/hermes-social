import argparse
import sqlite3
from pathlib import Path

from app.campaign_rules import CampaignRule, CampaignRuleStore

DEFAULT_KANBAN_DB = Path("/opt/data/kanban/boards/meta-campaigns/kanban.db")
DEFAULT_APP_DB = Path("data/processed_comments.sqlite3")


FIELD_ALIASES = {
    "statut métier": "status",
    "statut metier": "status",
    "plateforme": "platform",
    "platform": "platform",
    "media/post id": "media_id",
    "media id": "media_id",
    "post id": "media_id",
    "mots-clés": "keywords",
    "mots-cles": "keywords",
    "keywords": "keywords",
    "réponse publique": "public_reply_text",
    "reponse publique": "public_reply_text",
    "public reply": "public_reply_text",
    "message dm": "dm_text",
    "dm": "dm_text",
    "url ressource": "resource_url",
    "resource url": "resource_url",
}


def parse_campaign_card(task_id: str, title: str, body: str | None) -> CampaignRule | None:
    if title.strip().lower().startswith("template"):
        return None

    fields = _parse_fields(body or "")
    status = fields.get("status", "").strip().lower()
    if status != "actif":
        return None

    platform = fields.get("platform", "any").strip().lower() or "any"
    if platform not in {"instagram", "facebook", "any"}:
        platform = "any"

    keywords = [keyword.strip().lower() for keyword in fields.get("keywords", "").split(",") if keyword.strip()]
    public_reply_text = fields.get("public_reply_text", "").strip() or "C'est envoyé, check tes DM"
    dm_text = fields.get("dm_text", "").strip()
    resource_url = fields.get("resource_url", "").strip()
    if not dm_text and resource_url:
        dm_text = f"Voici la ressource demandée : {resource_url}"

    if not keywords or not dm_text:
        return None

    return CampaignRule(
        source_task_id=task_id,
        name=title.strip(),
        platform=platform,
        media_id=fields.get("media_id", "").strip() or None,
        keywords=keywords,
        public_reply_text=public_reply_text,
        dm_text=dm_text,
        enabled=True,
    )


def sync_kanban_campaigns(kanban_db: str | Path = DEFAULT_KANBAN_DB, app_db: str | Path = DEFAULT_APP_DB) -> int:
    kanban_db = Path(kanban_db)
    if not kanban_db.exists():
        raise FileNotFoundError(f"Kanban database not found: {kanban_db}")

    store = CampaignRuleStore(app_db)
    synced = 0
    with sqlite3.connect(kanban_db) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        order_by = "created_at" if "created_at" in columns else "id"
        rows = connection.execute(
            f"SELECT id, title, body FROM tasks WHERE status != 'archived' ORDER BY {order_by}"
        ).fetchall()

    for task_id, title, body in rows:
        rule = parse_campaign_card(task_id, title, body)
        if rule is None:
            continue
        store.upsert_rule(rule)
        synced += 1
    store.disable_rules_except({task_id for task_id, title, body in rows if parse_campaign_card(task_id, title, body) is not None})
    return synced


def _parse_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            normalized_key = FIELD_ALIASES.get(key.strip().lower())
            if normalized_key:
                fields[normalized_key] = value.strip()
                current_key = normalized_key
                continue
        if current_key and current_key in {"dm_text", "public_reply_text"}:
            fields[current_key] = f"{fields[current_key]}\n{line}".strip()
    return fields


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Hermes Kanban campaign cards to the app SQLite database.")
    parser.add_argument("--kanban-db", type=Path, default=DEFAULT_KANBAN_DB)
    parser.add_argument("--db", type=Path, default=DEFAULT_APP_DB)
    args = parser.parse_args()

    synced = sync_kanban_campaigns(args.kanban_db, args.db)
    print(f"Synced {synced} campaign rule(s)")


if __name__ == "__main__":
    main()
