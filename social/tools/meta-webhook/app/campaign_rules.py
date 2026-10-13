import json
from dataclasses import dataclass
from typing import Literal

from app.keyword import contains_keyword
from app.sqlite_store import SQLiteStore
from app.webhook_parser import CommentEvent


@dataclass(frozen=True)
class CampaignRule:
    source_task_id: str
    name: str
    platform: str
    media_id: str | None
    keywords: list[str]
    public_reply_text: str
    dm_text: str
    enabled: bool = True
    delivery_mode: str = "direct"
    # s13 (ADR 039): "kanban" (synced from the Hermes kanban) or "console" (owned by the console).
    source: str = "kanban"


UpsertOutcome = Literal["created", "updated", "skipped_console"]


class CampaignRuleStore(SQLiteStore):

    def upsert_rule(self, rule: CampaignRule) -> UpsertOutcome:
        """Insert or update a rule coming from the kanban sync.

        A row owned by the console (`source = 'console'`, ADR 039) is never rewritten:
        the read and the write happen in the same IMMEDIATE transaction, and the
        ON CONFLICT ... WHERE guard is a second line of defence.
        """
        connection = self._connect()
        try:
            connection.isolation_level = None
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT source FROM campaign_rules WHERE source_task_id = ?", (rule.source_task_id,)
            ).fetchone()
            if existing is not None and existing[0] == "console":
                connection.execute("COMMIT")
                return "skipped_console"
            connection.execute(
                """
                INSERT INTO campaign_rules (
                    source_task_id, name, platform, media_id, keywords_json,
                    public_reply_text, dm_text, enabled, delivery_mode, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source_task_id) DO UPDATE SET
                    name = excluded.name,
                    platform = excluded.platform,
                    media_id = excluded.media_id,
                    keywords_json = excluded.keywords_json,
                    public_reply_text = excluded.public_reply_text,
                    dm_text = excluded.dm_text,
                    enabled = excluded.enabled,
                    delivery_mode = excluded.delivery_mode,
                    updated_at = CURRENT_TIMESTAMP
                WHERE campaign_rules.source != 'console'
                """,
                (
                    rule.source_task_id,
                    rule.name,
                    rule.platform,
                    rule.media_id,
                    json.dumps(rule.keywords, ensure_ascii=False),
                    rule.public_reply_text,
                    rule.dm_text,
                    int(rule.enabled),
                    rule.delivery_mode,
                    rule.source,
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return "created" if existing is None else "updated"

    def list_rules(self) -> list[CampaignRule]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_task_id, name, platform, media_id, keywords_json,
                       public_reply_text, dm_text, enabled, delivery_mode, source
                FROM campaign_rules
                ORDER BY name
                """
            ).fetchall()
        return [self._rule_from_row(row) for row in rows]

    def has_enabled_rules(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM campaign_rules WHERE enabled = 1 LIMIT 1").fetchone()
        return row is not None

    def has_any_rule(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM campaign_rules LIMIT 1").fetchone()
        return row is not None

    def disable_rules_except(self, active_source_task_ids: set[str]) -> None:
        # Console-owned rows (ADR 039) are never disabled by the kanban sync, in both branches.
        with self._connect() as connection:
            if not active_source_task_ids:
                connection.execute(
                    "UPDATE campaign_rules SET enabled = 0, updated_at = CURRENT_TIMESTAMP WHERE source != 'console'"
                )
                return
            placeholders = ", ".join("?" for _ in active_source_task_ids)
            connection.execute(
                f"""
                UPDATE campaign_rules
                SET enabled = 0, updated_at = CURRENT_TIMESTAMP
                WHERE source_task_id NOT IN ({placeholders})
                  AND source != 'console'
                """,
                tuple(active_source_task_ids),
            )

    def find_matching_rule(self, event: CommentEvent) -> CampaignRule | None:
        candidates = []
        for rule in self.list_rules():
            if not rule.enabled:
                continue
            if rule.platform not in {"any", event.platform}:
                continue
            if rule.media_id and event.media_id not in self._media_ids(rule):
                continue
            if not any(contains_keyword(event.text, keyword) for keyword in rule.keywords):
                continue
            candidates.append(rule)

        if not candidates:
            return None

        return sorted(candidates, key=lambda rule: self._specificity(rule, event), reverse=True)[0]

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS campaign_rules (
                    source_task_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    media_id TEXT,
                    keywords_json TEXT NOT NULL,
                    public_reply_text TEXT NOT NULL,
                    dm_text TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    delivery_mode TEXT NOT NULL DEFAULT 'direct',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(campaign_rules)")}
            if "delivery_mode" not in columns:
                connection.execute("ALTER TABLE campaign_rules ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'direct'")
            if "source" not in columns:
                connection.execute("ALTER TABLE campaign_rules ADD COLUMN source TEXT NOT NULL DEFAULT 'kanban'")

    def _rule_from_row(self, row: tuple) -> CampaignRule:
        return CampaignRule(
            source_task_id=row[0],
            name=row[1],
            platform=row[2],
            media_id=row[3],
            keywords=list(json.loads(row[4])),
            public_reply_text=row[5],
            dm_text=row[6],
            enabled=bool(row[7]),
            delivery_mode=row[8] if len(row) > 8 and row[8] else "direct",
            source=row[9] if len(row) > 9 and row[9] else "kanban",
        )

    def _specificity(self, rule: CampaignRule, event: CommentEvent) -> tuple[int, int]:
        media_score = 1 if rule.media_id and event.media_id in self._media_ids(rule) else 0
        platform_score = 1 if rule.platform == event.platform else 0
        return media_score, platform_score

    def _media_ids(self, rule: CampaignRule) -> set[str]:
        if not rule.media_id:
            return set()
        return {media_id.strip() for media_id in rule.media_id.split(",") if media_id.strip()}
