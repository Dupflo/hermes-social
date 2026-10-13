import json
import sqlite3
import time

import pytest

from app import webhook_delivery_store as module
from app.webhook_delivery_store import (
    DeliverySummary,
    WebhookDeliveryStore,
    summarize_payload,
    summarize_raw,
)

DAY_MS = 24 * 60 * 60 * 1000
NOW = 1_760_000_000_000


@pytest.fixture(autouse=True)
def _fresh_state():
    module._reset_purge_state()
    yield
    module._reset_purge_state()


def _rows(database):
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT received_at, object, fields, result, status_code, latency_ms, entries "
            "FROM webhook_deliveries ORDER BY id"
        ).fetchall()


def _insert_old(database, received_at):
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "INSERT INTO webhook_deliveries (received_at, object, fields, result, status_code, latency_ms, entries) "
            "VALUES (?, 'instagram', '', 'accepted', 200, 1, 0)",
            (received_at,),
        )
    connection.close()


def test_schema_has_exact_columns_and_indexes(tmp_path):
    database = tmp_path / "processed.sqlite3"
    WebhookDeliveryStore(database)

    with sqlite3.connect(database) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(webhook_deliveries)")]
        indexes = sorted(row[1] for row in connection.execute("PRAGMA index_list(webhook_deliveries)"))
        indexed = {
            name: [row[2] for row in connection.execute(f"PRAGMA index_info({name})")] for name in indexes
        }

    assert columns == [
        "id",
        "received_at",
        "object",
        "fields",
        "result",
        "status_code",
        "latency_ms",
        "entries",
    ]
    assert indexes == ["idx_webhook_deliveries_object_received", "idx_webhook_deliveries_received"]
    assert indexed["idx_webhook_deliveries_object_received"] == ["object", "received_at"]
    assert indexed["idx_webhook_deliveries_received"] == ["received_at"]


def test_check_constraint_refuses_unknown_result(tmp_path):
    database = tmp_path / "processed.sqlite3"
    WebhookDeliveryStore(database)

    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO webhook_deliveries (received_at, object, result, status_code, latency_ms) "
                "VALUES (1, 'instagram', 'weird', 200, 1)"
            )


def test_record_refuses_result_outside_results(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "processed.sqlite3")
    with pytest.raises(ValueError):
        store.record(DeliverySummary("instagram", "", 0), "weird", 200, 1, NOW)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"object": "instagram", "entry": [{"changes": [{"field": "comments"}]}]},
            DeliverySummary("instagram", "comments", 1),
        ),
        ({"object": "page", "entry": [{}, {}]}, DeliverySummary("page", "", 2)),
        ({"object": "whatsapp_business_account", "entry": []}, DeliverySummary("other", "", 0)),
        ({"object": "<script>"}, DeliverySummary("other", "", 0)),
        ([{"object": "instagram"}], DeliverySummary("unknown", "", 0)),
        (None, DeliverySummary("unknown", "", 0)),
        (
            {
                "object": "page",
                "entry": [
                    {"messaging": [{}]},
                    {"changes": [{"field": "feed"}, {"field": "comments"}]},
                ],
            },
            DeliverySummary("page", "comments,feed,messaging", 2),
        ),
        (
            {"object": "instagram", "entry": [{"changes": [{"field": "Field-X"}, {"field": "a" * 41}]}]},
            DeliverySummary("instagram", "", 1),
        ),
        ({"object": "instagram", "entry": "nope"}, DeliverySummary("instagram", "", 0)),
    ],
)
def test_summarize_payload_normalizes(payload, expected):
    assert summarize_payload(payload) == expected


def test_summarize_payload_keeps_ten_sorted_fields_at_most():
    names = [f"field_{chr(ord('a') + i)}" for i in range(15)]
    payload = {"object": "instagram", "entry": [{"changes": [{"field": n} for n in reversed(names)]}]}

    summary = summarize_payload(payload)

    assert summary.fields == ",".join(sorted(names)[:10])


def test_summarize_payload_caps_entries():
    payload = {"object": "page", "entry": [{} for _ in range(1500)]}
    assert summarize_payload(payload).entries == 1000


def test_summarize_raw_never_raises():
    assert summarize_raw(b"{not json") == DeliverySummary("unknown", "", 0)
    assert summarize_raw(b"\xff\xfe") == DeliverySummary("unknown", "", 0)
    assert summarize_raw(b"null") == DeliverySummary("unknown", "", 0)
    assert summarize_raw(b"[]") == DeliverySummary("unknown", "", 0)
    assert summarize_raw(json.dumps({"object": "instagram", "entry": []}).encode()) == DeliverySummary(
        "instagram", "", 0
    )


def test_record_writes_passed_values_and_nothing_else(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = WebhookDeliveryStore(database)

    store.record(DeliverySummary("page", "feed,messaging", 3), "signature_rejected", 403, 12, NOW)

    assert _rows(database) == [(NOW, "page", "feed,messaging", "signature_rejected", 403, 12, 3)]


def test_purge_removes_rows_older_than_thirty_days(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = WebhookDeliveryStore(database)
    _insert_old(database, NOW - 30 * DAY_MS - 1)
    _insert_old(database, NOW - 30 * DAY_MS)

    deleted = store.purge(NOW)

    assert deleted == 1
    assert [row[0] for row in _rows(database)] == [NOW - 30 * DAY_MS]


def test_purge_is_throttled_to_once_every_ten_minutes(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = WebhookDeliveryStore(database)
    summary = DeliverySummary("instagram", "", 0)
    old = NOW - 40 * DAY_MS

    store.record(summary, "accepted", 200, 1, NOW)
    _insert_old(database, old)
    WebhookDeliveryStore(database).record(summary, "accepted", 200, 1, NOW + 10 * 60 * 1000 - 1)
    assert old in [row[0] for row in _rows(database)]

    WebhookDeliveryStore(database).record(summary, "accepted", 200, 1, NOW + 10 * 60 * 1000)
    assert old not in [row[0] for row in _rows(database)]
    assert len(_rows(database)) == 3


def test_purge_deletes_by_bounded_batches(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "PURGE_BATCH", 5)
    database = tmp_path / "processed.sqlite3"
    store = WebhookDeliveryStore(database)
    for i in range(7):
        _insert_old(database, NOW - 40 * DAY_MS + i)

    assert store.purge(NOW) == 5
    assert store.purge(NOW) == 2
    assert store.purge(NOW) == 0


def test_schema_is_ensured_once_per_process_and_path(tmp_path, monkeypatch):
    database = tmp_path / "processed.sqlite3"
    WebhookDeliveryStore(database)
    calls = []
    original = WebhookDeliveryStore._connect

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(WebhookDeliveryStore, "_connect", counting)
    WebhookDeliveryStore(database)
    assert calls == []

    module._reset_purge_state()
    WebhookDeliveryStore(database)
    assert calls == [1]


def test_record_gives_up_fast_when_the_database_is_locked(tmp_path):
    database = tmp_path / "processed.sqlite3"
    store = WebhookDeliveryStore(database)
    locker = sqlite3.connect(database, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.record(DeliverySummary("instagram", "", 0), "accepted", 200, 1, NOW)
        assert time.monotonic() - started < 2
    finally:
        locker.execute("ROLLBACK")
        locker.close()
