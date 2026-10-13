import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.main import create_app


def test_webhook_verification_returns_challenge_when_verify_token_matches(monkeypatch):
    monkeypatch.setenv("META_VERIFY_TOKEN", "test-verify-token")
    client = TestClient(create_app())

    response = client.get(
        "/webhook/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-verify-token",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 200
    assert response.text == "challenge-123"


def test_webhook_verification_rejects_wrong_verify_token(monkeypatch):
    monkeypatch.setenv("META_VERIFY_TOKEN", "test-verify-token")
    client = TestClient(create_app())

    response = client.get(
        "/webhook/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 403


def test_privacy_policy_page_is_public():
    client = TestClient(create_app())

    response = client.get("/privacy")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Politique de confidentialité" in response.text
    assert "contact@dupuisweb.com" in response.text


def test_data_deletion_page_is_public():
    client = TestClient(create_app())

    response = client.get("/data-deletion")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Instructions de suppression des données" in response.text
    assert "contact@dupuisweb.com" in response.text


def test_meta_auth_callback_page_exists():
    client = TestClient(create_app())

    response = client.get("/auth/meta/callback")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Connexion Meta reçue" in response.text


def test_meta_deauthorize_callbacks_exist():
    client = TestClient(create_app())

    get_response = client.get("/auth/meta/deauthorize")
    post_response = client.post("/auth/meta/deauthorize")

    assert get_response.status_code == 200
    assert get_response.json() == {"status": "ok"}
    assert post_response.status_code == 200
    assert post_response.json() == {"status": "ok"}


def test_webhook_post_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    client = TestClient(create_app())

    response = client.post(
        "/webhook/meta",
        json={"object": "instagram", "entry": []},
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 403


def test_webhook_post_accepts_valid_signature_and_ignores_non_matching_keyword(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("RESOURCE_KEYWORD", "proxy")
    body = {"object": "instagram", "entry": []}
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()
    client = TestClient(create_app())

    response = client.post(
        "/webhook/meta",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "processed": 0,
        "manual_review_replies": 0,
        "manual_review_enqueued": 0,
        "private_messages_enqueued": 0,
        "results": [],
        "private_message_results": [],
    }


def test_webhook_post_reconciles_manual_owner_reply_into_review_queue(monkeypatch, tmp_path):
    db = tmp_path / "app.sqlite3"
    review_store = CommentReviewStore(db)
    review_store.upsert_pending(
        CommentReviewItem(
            platform="facebook",
            comment_id="post-1_comment-1",
            media_id="post-1",
            username="alice",
            text="Le lien svp",
            media_permalink="https://facebook.test/reel/1",
            media_caption="caption",
            reason="direct_request",
            score=80,
        )
    )
    review_store.mark_in_review("facebook", "post-1_comment-1")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_PAGE_ID", "page-1")
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(db))
    body = {
        "object": "page",
        "entry": [
            {
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "comment_id": "reply-1",
                            "parent_id": "post-1_comment-1",
                            "post_id": "post-1",
                            "message": "Réponse manuelle",
                            "from": {"id": "page-1", "name": "FloDev"},
                        },
                    }
                ]
            }
        ],
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    response = TestClient(create_app()).post(
        "/webhook/meta",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
    )

    assert response.status_code == 200
    assert CommentReviewStore(db).get("facebook", "post-1_comment-1").status == "manually_replied"


def test_webhook_post_enqueues_interesting_non_campaign_comment(monkeypatch, tmp_path):
    db = tmp_path / "app.sqlite3"
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_PAGE_ID", "page-1")
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(db))
    body = {
        "object": "instagram",
        "entry": [
            {
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "ig-comment-1",
                            "media_id": "ig-media-1",
                            "text": "Le nom du site svp",
                            "from": {"id": "user-1", "username": "alice"},
                        },
                    }
                ]
            }
        ],
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    response = TestClient(create_app()).post(
        "/webhook/meta",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["manual_review_enqueued"] == 1
    item = CommentReviewStore(db).get("instagram", "ig-comment-1")
    assert item.status == "pending"
    assert item.reason == "direct_request"


def test_webhook_post_enqueues_user_reply_inside_existing_thread(monkeypatch, tmp_path):
    db = tmp_path / "app.sqlite3"
    review_store = CommentReviewStore(db)
    review_store.upsert_pending(
        CommentReviewItem(
            platform="facebook",
            comment_id="post-1_comment-1",
            media_id="post-1",
            username="alice",
            text="Repo ?",
            media_permalink="https://facebook.test/reel/1",
            media_caption="caption",
            reason="direct_request",
            score=80,
        )
    )
    review_store.mark_replied("facebook", "post-1_comment-1", posted_reply_id="page-reply-1")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_PAGE_ID", "page-1")
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(db))
    body = {
        "object": "page",
        "entry": [
            {
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "comment_id": "reply-2",
                            "parent_id": "post-1_comment-1",
                            "post_id": "post-1",
                            "message": "Merci, mais comment on l'installe ?",
                            "from": {"id": "user-1", "name": "Alice"},
                        },
                    }
                ]
            }
        ],
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    response = TestClient(create_app()).post(
        "/webhook/meta",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
    )

    assert response.status_code == 200
    assert response.json()["manual_review_enqueued"] == 1
    item = CommentReviewStore(db).get("facebook", "reply-2")
    assert item is not None
    assert item.status == "pending"
    assert item.reason == "thread_reply"
    assert item.media_id == "post-1"


def test_webhook_post_enqueues_private_message_without_auto_reply(monkeypatch, tmp_path):
    db = tmp_path / "app.sqlite3"
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_PAGE_ID", "page-1")
    monkeypatch.setenv("PROCESSED_COMMENTS_DATABASE", str(db))
    body = {
        "object": "page",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "user-1"},
                        "recipient": {"id": "page-1"},
                        "timestamp": 1784976000000,
                        "message": {"mid": "mid-1", "text": "Salut, je veux le lien RPI"},
                    }
                ]
            }
        ],
    }
    raw_body = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    response = TestClient(create_app()).post(
        "/webhook/meta",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["manual_review_enqueued"] == 1
    assert body["processed"] == 0
    item = CommentReviewStore(db).get("facebook_dm", "mid-1")
    assert item is not None
    assert item.status == "pending"
    assert item.reason == "private_message"
    assert item.username == "user-1"
    assert item.text == "Salut, je veux le lien RPI"


def test_keyword_match_is_case_insensitive_and_word_based():
    from app.keyword import contains_keyword

    assert contains_keyword("Je veux Proxy svp", "proxy") is True
    assert contains_keyword("PROXY!", "proxy") is True
    assert contains_keyword("proxyman", "proxy") is False
    assert contains_keyword("sans proxy", "proxy") is False
    assert contains_keyword("pas de proxy", "proxy") is False


import pytest
from app.main import _hydrate_missing_instagram_media
from app.webhook_parser import CommentEvent


@pytest.mark.asyncio
async def test_hydrate_missing_instagram_media_resolves_media_id():
    class FakeGraphClient:
        async def get_instagram_comment_media(self, comment_id):
            assert comment_id == "ig-comment-without-media"
            return {"media": {"id": "ig-media-resolved"}}

    events = [
        CommentEvent(
            platform="instagram",
            comment_id="ig-comment-without-media",
            text="Outils",
            media_id=None,
            author_id="user-1",
            username="alice",
        ),
        CommentEvent(
            platform="facebook",
            comment_id="fb-comment",
            text="Outils",
            media_id="post-1",
        ),
    ]

    hydrated = await _hydrate_missing_instagram_media(events, FakeGraphClient())

    assert hydrated[0].media_id == "ig-media-resolved"
    assert hydrated[0].comment_id == "ig-comment-without-media"
    assert hydrated[1] == events[1]


def test_webhook_accepts_legacy_signature_when_sha256_header_is_invalid(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_PAGE_ACCESS_TOKEN", "token")
    monkeypatch.setenv("META_PAGE_ID", "page-1")
    monkeypatch.setenv("META_PAGE_IDS", "page-1")

    body = json.dumps({"object": "instagram", "entry": []}).encode()
    legacy = hmac.new(b"secret", body, hashlib.sha1).hexdigest()
    client = TestClient(create_app())

    response = client.post(
        "/webhook/meta",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=bad",
            "X-Hub-Signature": f"sha1={legacy}",
        },
    )

    assert response.status_code == 200


# --- s08-webhook-health: delivery telemetry -------------------------------------

import logging
import os
import sqlite3
import time

from app import main as main_module
from app.webhook_delivery_store import WebhookDeliveryStore

EMPTY_OK = {
    "status": "ok",
    "processed": 0,
    "manual_review_replies": 0,
    "manual_review_enqueued": 0,
    "private_messages_enqueued": 0,
    "results": [],
    "private_message_results": [],
}


def _deliveries_db():
    return os.environ["PROCESSED_COMMENTS_DATABASE"]


def _deliveries():
    connection = sqlite3.connect(_deliveries_db())
    try:
        return connection.execute(
            "SELECT object, fields, result, status_code, latency_ms, entries FROM webhook_deliveries ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def _signed_post(client, raw_body, secret=b"secret"):
    signature = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook/meta",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
    )


def _unsigned_post(client, raw_body):
    return client.post(
        "/webhook/meta",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
    )


def _raise_runtime(*_args, **_kwargs):
    raise RuntimeError("boom")


def test_delivery_accepted_is_recorded_without_changing_the_response(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    client = TestClient(create_app())

    response = _signed_post(client, json.dumps({"object": "instagram", "entry": []}).encode())

    assert response.status_code == 200
    assert response.json() == EMPTY_OK
    rows = _deliveries()
    assert [(r[0], r[2], r[3]) for r in rows] == [("instagram", "accepted", 200)]
    assert rows[0][4] >= 0


def test_delivery_page_messaging_is_counted_as_a_field(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    body = {"object": "page", "entry": [{"id": "page-1", "messaging": []}]}

    response = _signed_post(TestClient(create_app()), json.dumps(body).encode())

    assert response.status_code == 200
    assert _deliveries() == [("page", "messaging", "accepted", 200, _deliveries()[0][4], 1)]


def test_delivery_signature_rejected_is_recorded_with_normalized_object(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    client = TestClient(create_app())

    first = _unsigned_post(client, json.dumps({"object": "instagram", "entry": [{}]}).encode())
    second = _unsigned_post(client, json.dumps({"object": "<script>"}).encode())
    third = _unsigned_post(client, b"{not json")

    for response in (first, second, third):
        assert response.status_code == 403
        assert response.json() == {"detail": "Invalid Meta signature"}
    assert [(r[0], r[2], r[3], r[5]) for r in _deliveries()] == [
        ("instagram", "signature_rejected", 403, 1),
        ("other", "signature_rejected", 403, 0),
        ("unknown", "signature_rejected", 403, 0),
    ]


def test_delivery_error_is_recorded_and_the_500_is_unchanged(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setattr(main_module, "parse_comment_events", _raise_runtime)
    body = json.dumps({"object": "instagram", "entry": []}).encode()

    response = _signed_post(TestClient(create_app(), raise_server_exceptions=False), body)
    not_json = _signed_post(TestClient(create_app(), raise_server_exceptions=False), b"{not json")

    monkeypatch.setattr(main_module, "_record_delivery", lambda *a, **k: None)
    reference = _signed_post(TestClient(create_app(), raise_server_exceptions=False), body)

    assert reference.status_code == 500
    for got in (response, not_json):
        assert got.status_code == reference.status_code
        assert got.text == reference.text == "Internal Server Error"
    assert [(r[0], r[2], r[3]) for r in _deliveries()] == [
        ("instagram", "error", 500),
        ("unknown", "error", 500),
    ]


def test_delivery_row_never_contains_the_body_or_the_signature(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    marker = "MARKER-7f3c9a"
    body = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-user-1",
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "ig-comment-marker",
                            "media_id": "ig-media-marker",
                            "text": f"hello {marker}",
                            "from": {"id": "user-9", "username": "bob"},
                        },
                    }
                ],
            }
        ],
    }
    raw_body = json.dumps(body).encode()
    signature = hmac.new(b"secret", raw_body, hashlib.sha256).hexdigest()

    response = _signed_post(TestClient(create_app()), raw_body)

    assert response.status_code == 200
    connection = sqlite3.connect(_deliveries_db())
    try:
        rows = connection.execute("SELECT * FROM webhook_deliveries").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    dumped = json.dumps(rows)
    for forbidden in (marker, signature, "sha256=", "ig-comment-marker", "ig-media-marker", "user-9"):
        assert forbidden not in dumped


def test_delivery_store_failure_never_changes_the_response(monkeypatch, caplog):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    marker = "MARKER-store-down"
    accepted_body = json.dumps({"object": "instagram", "entry": []}).encode()
    rejected_body = json.dumps({"object": "instagram", "note": marker}).encode()

    def exchange():
        client = TestClient(create_app(), raise_server_exceptions=False)
        accepted = _signed_post(client, accepted_body)
        rejected = _unsigned_post(client, rejected_body)
        with monkeypatch.context() as patch:
            patch.setattr(main_module, "parse_comment_events", _raise_runtime)
            failed = _signed_post(client, accepted_body)
        return [(r.status_code, r.content) for r in (accepted, rejected, failed)]

    reference = exchange()

    def broken_record(self, *args, **kwargs):
        raise sqlite3.OperationalError(f"database is locked {marker}")

    monkeypatch.setattr(WebhookDeliveryStore, "record", broken_record)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        degraded = exchange()

    assert degraded == reference
    assert [code for code, _ in degraded] == [200, 403, 500]
    assert caplog.text.count("webhook_delivery_record_failed OperationalError") == 3
    assert marker not in caplog.text


def test_delivery_lock_never_delays_the_403(monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", "secret")
    WebhookDeliveryStore(_deliveries_db())
    locker = sqlite3.connect(_deliveries_db(), isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        response = _unsigned_post(TestClient(create_app()), json.dumps({"object": "instagram"}).encode())
        elapsed = time.monotonic() - started
    finally:
        locker.execute("ROLLBACK")
        locker.close()

    assert response.status_code == 403
    assert elapsed < 3


def test_create_app_alone_does_not_create_the_database():
    create_app()
    assert not os.path.exists(_deliveries_db())


def test_startup_creates_the_deliveries_table_before_any_request():
    with TestClient(create_app()):
        connection = sqlite3.connect(_deliveries_db())
        try:
            tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            indexes = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")]
        finally:
            connection.close()
    assert "webhook_deliveries" in tables
    assert "idx_webhook_deliveries_object_received" in indexes
    assert "idx_webhook_deliveries_received" in indexes


def test_startup_failure_never_prevents_the_app_from_serving(monkeypatch, caplog):
    class BrokenStore:
        def __init__(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(main_module, "WebhookDeliveryStore", BrokenStore)
    with caplog.at_level(logging.WARNING, logger="app.main"):
        with TestClient(create_app()) as client:
            response = client.get("/health")

    assert response.status_code == 200
    assert "webhook_delivery_startup_failed OperationalError" in caplog.text
