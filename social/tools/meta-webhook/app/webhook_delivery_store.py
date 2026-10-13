"""Telemetry of the Meta webhook deliveries (s08-webhook-health).

One row per POST /webhook/meta: when it arrived, the normalized `object`, the
field names, the result, the HTTP status and the processing latency. Nothing of
the request itself is kept (no body, no request metadata, no Meta identifier):
the summary below is computed from an unauthenticated body, so it is reduced to
a closed list of objects and a filtered, bounded list of field names.

The table lives in the same `processed_comments.sqlite3` as the other stores and
is read (read-only) by hermes-console. Rows older than 30 days are purged here,
by this writer, in bounded batches.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.sqlite_store import SQLiteStore

RESULTS = ("accepted", "signature_rejected", "error")

RETENTION_MS = 30 * 24 * 60 * 60 * 1000
PURGE_EVERY_MS = 10 * 60 * 1000
PURGE_BATCH = 5000
# Never inherit the 30 s of SQLiteStore._connect: Meta waits for the answer.
CONNECT_TIMEOUT_S = 1.0

KNOWN_OBJECTS = ("instagram", "page")
MAX_FIELDS = 10
MAX_ENTRIES = 1000
_FIELD_NAME = re.compile(r"^[a-z_]{1,40}$")


@dataclass(frozen=True)
class DeliverySummary:
    object: str
    fields: str
    entries: int


_UNKNOWN = DeliverySummary("unknown", "", 0)


def summarize_payload(payload: object) -> DeliverySummary:
    if not isinstance(payload, dict):
        return _UNKNOWN
    raw_object = payload.get("object")
    obj = raw_object if raw_object in KNOWN_OBJECTS else "other"
    entry = payload.get("entry")
    if not isinstance(entry, list):
        return DeliverySummary(obj, "", 0)
    names: set[str] = set()
    for item in entry[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        changes = item.get("changes")
        if isinstance(changes, list):
            for change in changes:
                field = change.get("field") if isinstance(change, dict) else None
                if isinstance(field, str) and _FIELD_NAME.match(field):
                    names.add(field)
        if "messaging" in item:
            names.add("messaging")
    fields = ",".join(sorted(names)[:MAX_FIELDS])
    return DeliverySummary(obj, fields, min(len(entry), MAX_ENTRIES))


def summarize_raw(body: bytes) -> DeliverySummary:
    try:
        payload = json.loads(body)
    except Exception:
        return _UNKNOWN
    return summarize_payload(payload)


@dataclass
class _PathState:
    schema_ready: bool = False
    last_purge_ms: int | None = None


# Stores are instantiated on every request: the per-file state lives here.
_STATE: dict[str, _PathState] = {}


def _reset_purge_state() -> None:
    _STATE.clear()


class WebhookDeliveryStore(SQLiteStore):
    def __init__(self, database_path: str | Path) -> None:
        self._key = str(Path(database_path).resolve())
        super().__init__(database_path)

    def _state(self) -> _PathState:
        return _STATE.setdefault(self._key, _PathState())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=CONNECT_TIMEOUT_S)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _ensure_schema(self) -> None:
        if self._state().schema_ready:
            return
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    received_at INTEGER NOT NULL,
                    object TEXT NOT NULL,
                    fields TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL CHECK (result IN ('accepted', 'signature_rejected', 'error')),
                    status_code INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    entries INTEGER NOT NULL DEFAULT 0
                )
                """
                )
                connection.execute(
                    """
                CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_object_received
                ON webhook_deliveries(object, received_at)
                """
                )
                connection.execute(
                    """
                CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received
                ON webhook_deliveries(received_at)
                """
                )
        finally:
            connection.close()
        self._state().schema_ready = True

    def record(
        self,
        summary: DeliverySummary,
        result: str,
        status_code: int,
        latency_ms: int,
        now_ms: int,
    ) -> None:
        if result not in RESULTS:
            raise ValueError(f"unknown delivery result: {result}")
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO webhook_deliveries
                        (received_at, object, fields, result, status_code, latency_ms, entries)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (now_ms, summary.object, summary.fields, result, status_code, latency_ms, summary.entries),
                )
        finally:
            connection.close()
        # The row is committed before the purge: a failing purge never loses it.
        self.maybe_purge(now_ms)

    def purge(self, now_ms: int) -> int:
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM webhook_deliveries WHERE id IN (
                        SELECT id FROM webhook_deliveries
                        WHERE received_at < ?
                        ORDER BY received_at
                        LIMIT ?
                    )
                    """,
                    (now_ms - RETENTION_MS, PURGE_BATCH),
                )
                return cursor.rowcount
        finally:
            connection.close()

    def maybe_purge(self, now_ms: int) -> int:
        state = self._state()
        if state.last_purge_ms is not None and now_ms - state.last_purge_ms < PURGE_EVERY_MS:
            return 0
        state.last_purge_ms = now_ms
        return self.purge(now_ms)
