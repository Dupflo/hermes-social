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
