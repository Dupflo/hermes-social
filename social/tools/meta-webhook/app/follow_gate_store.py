from dataclasses import dataclass

from app.sqlite_store import SQLiteStore


FOLLOW_GATE_CONFIRMATION_PAYLOAD = "FOLLOW_GATE_CONFIRMED"
FOLLOW_GATE_CONFIRMATION_TEXTS = {
    "ok",
    "okay",
    "fait",
    "c'est fait",
    "cest fait",
    "je suis abonné",
    "je suis abonne",
    "j’ai suivi",
    "j'ai suivi",
    "jai suivi",
    "✅ j’ai suivi",
    "✅ j'ai suivi",
    "✅ jai suivi",
    "suivi",
}


@dataclass(frozen=True)
class FollowGate:
    platform: str
    comment_id: str
    sender_id: str
    campaign: str
    keyword: str
    dm_text: str
    status: str


def is_follow_gate_confirmation(text: str) -> bool:
    normalized = " ".join((text or "").strip().lower().split())
    return normalized == FOLLOW_GATE_CONFIRMATION_PAYLOAD.lower() or normalized in FOLLOW_GATE_CONFIRMATION_TEXTS


def follow_gate_prompt_text() -> str:
    return (
        "Hello 👋\n\n"
        "Trop cool que ça t’intéresse 🙌 Je réserve ce contenu aux personnes abonnées. "
        "Abonne-toi au compte, puis réponds « ✅ J’ai suivi » et je t’envoie le lien."
    )


def follow_gate_quick_replies() -> list[dict[str, str]]:
    return [
        {
            "content_type": "text",
            "title": "✅ J’ai suivi",
            "payload": FOLLOW_GATE_CONFIRMATION_PAYLOAD,
        }
    ]


class FollowGateStore(SQLiteStore):
    def create_pending(
        self,
        *,
        platform: str,
        comment_id: str,
        sender_id: str,
        campaign: str,
        keyword: str,
        dm_text: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO follow_gates (
                    platform, comment_id, sender_id, campaign, keyword, dm_text, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'awaiting_follow_confirm', CURRENT_TIMESTAMP)
                ON CONFLICT(platform, comment_id) DO UPDATE SET
                    sender_id = excluded.sender_id,
                    campaign = excluded.campaign,
                    keyword = excluded.keyword,
                    dm_text = excluded.dm_text,
                    status = CASE
                        WHEN follow_gates.status = 'unlocked' THEN follow_gates.status
                        ELSE 'awaiting_follow_confirm'
                    END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (platform, comment_id, sender_id, campaign, keyword, dm_text),
            )

    def pending_for_sender(self, platform: str, sender_id: str) -> FollowGate | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT platform, comment_id, sender_id, campaign, keyword, dm_text, status
                FROM follow_gates
                WHERE platform = ? AND sender_id = ? AND status = 'awaiting_follow_confirm'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (platform, sender_id),
            ).fetchone()
        return self._gate_from_row(row) if row else None

    def mark_unlocked(self, platform: str, comment_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE follow_gates
                SET status = 'unlocked', unlocked_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE platform = ? AND comment_id = ?
                """,
                (platform, comment_id),
            )

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS follow_gates (
                    platform TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    campaign TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    dm_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'awaiting_follow_confirm',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    unlocked_at TEXT,
                    PRIMARY KEY (platform, comment_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_follow_gates_sender
                ON follow_gates(platform, sender_id, status, updated_at)
                """
            )

    def _gate_from_row(self, row: tuple) -> FollowGate:
        return FollowGate(
            platform=row[0],
            comment_id=row[1],
            sender_id=row[2],
            campaign=row[3],
            keyword=row[4],
            dm_text=row[5],
            status=row[6],
        )
