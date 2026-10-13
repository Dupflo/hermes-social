"""s09 — reading a comment back from Graph and rebuilding the event to replay."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.graph_client import GraphAPIError, GraphClient
from app.replay import (
    META_WINDOW,
    build_replay_event,
    facebook_media_id,
    graph_comment_age_exceeded,
)
from app.webhook_parser import CommentEvent


NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)


def _graph_date(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")


async def _get_comment(platform: str, comment_id: str, *, status: int = 200, body: dict | None = None):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": comment_id})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client, api_version="v21.0")
        result = await client.get_comment(comment_id, platform)
    return result, requests


def test_settings_has_an_empty_console_internal_token_by_default(monkeypatch):
    monkeypatch.delenv("CONSOLE_INTERNAL_TOKEN", raising=False)
    assert Settings(_env_file=None).console_internal_token == ""


def test_settings_reads_console_internal_token_from_env(monkeypatch):
    monkeypatch.setenv("CONSOLE_INTERNAL_TOKEN", "abc")
    assert Settings(_env_file=None).console_internal_token == "abc"


@pytest.mark.asyncio
async def test_get_comment_instagram_requests_exact_fields():
    result, requests = await _get_comment("instagram", "1789", body={"id": "1789", "text": "guide"})

    assert result == {"id": "1789", "text": "guide"}
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url).startswith("https://graph.facebook.com/v21.0/1789?")
    assert request.url.params["fields"] == "text,timestamp,username,from,media{id}"
    assert request.url.params["access_token"] == "page-token"


@pytest.mark.asyncio
async def test_get_comment_facebook_requests_exact_fields():
    _, requests = await _get_comment("facebook", "123_456")

    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v21.0/123_456"
    assert request.url.params["fields"] == "message,created_time,from"
    assert request.url.params["access_token"] == "page-token"


@pytest.mark.asyncio
async def test_get_comment_raises_graph_api_error_like_other_calls():
    with pytest.raises(GraphAPIError) as caught:
        await _get_comment(
            "instagram",
            "1789",
            status=400,
            body={"error": {"message": "Unsupported get request", "code": 100, "error_subcode": 33}},
        )
    assert caught.value.code == 100


def test_build_instagram_event_complete():
    data = {
        "id": "1789",
        "text": "GUIDE stp",
        "timestamp": _graph_date(NOW),
        "username": "alice",
        "from": {"id": "ig-42", "username": "alice"},
        "media": {"id": "media-7"},
    }

    assert build_replay_event("instagram", "1789", data, "999") == CommentEvent(
        platform="instagram",
        comment_id="1789",
        text="GUIDE stp",
        media_id="media-7",
        author_id="ig-42",
        username="alice",
    )


def test_build_instagram_event_falls_back_on_from_username():
    data = {"text": "x", "from": {"id": "ig-42", "username": "bob"}, "media": {"id": "m"}}
    event = build_replay_event("instagram", "1789", data, "999")
    assert event is not None and event.username == "bob"


def test_build_instagram_event_without_author_id_is_refused():
    data = {"text": "GUIDE", "timestamp": _graph_date(NOW), "username": "alice", "media": {"id": "m"}}
    assert build_replay_event("instagram", "1789", data, "999") is None
    assert build_replay_event("instagram", "1789", {**data, "from": {}}, "999") is None


def test_build_instagram_event_without_text_is_refused():
    data = {"from": {"id": "ig-42"}, "media": {"id": "m"}}
    assert build_replay_event("instagram", "1789", data, "999") is None


def test_build_facebook_event_derives_media_id_from_compound_comment_id():
    data = {"message": "PROMO", "created_time": _graph_date(NOW), "from": {"id": "fb-1", "name": "Carol"}}

    assert build_replay_event("facebook", "123_456", data, "999") == CommentEvent(
        platform="facebook",
        comment_id="123_456",
        text="PROMO",
        media_id="999_123",
        author_id="fb-1",
        username="Carol",
    )


def test_build_facebook_event_without_author_is_accepted():
    event = build_replay_event("facebook", "123_456", {"message": "PROMO"}, "999")
    assert event == CommentEvent(platform="facebook", comment_id="123_456", text="PROMO", media_id="999_123")


def test_build_facebook_event_without_text_is_refused():
    assert build_replay_event("facebook", "123_456", {"from": {"id": "fb-1"}}, "999") is None


def test_facebook_media_id_shape():
    assert facebook_media_id("123_456", "999") == "999_123"
    assert facebook_media_id("456", "999") is None
    assert facebook_media_id("123_456", "") is None


@pytest.mark.parametrize(
    ("platform", "field"),
    [("instagram", "timestamp"), ("facebook", "created_time")],
)
def test_graph_comment_age_strict_seven_days(platform, field):
    def age(delta: timedelta):
        return graph_comment_age_exceeded({field: _graph_date(NOW - delta)}, platform, NOW)

    assert META_WINDOW == timedelta(days=7)
    assert age(timedelta(days=7) - timedelta(seconds=1)) is False
    assert age(timedelta(days=7)) is False
    assert age(timedelta(days=7) + timedelta(seconds=1)) is True
    assert age(timedelta(hours=1)) is False


@pytest.mark.parametrize(
    ("platform", "data"),
    [
        ("instagram", {}),
        ("instagram", {"timestamp": None}),
        ("instagram", {"timestamp": "yesterday"}),
        ("instagram", {"created_time": "2026-10-13T11:00:00+0000"}),
        ("facebook", {"created_time": ""}),
        ("facebook", {"timestamp": "2026-10-13T11:00:00+0000"}),
    ],
)
def test_graph_comment_age_missing_or_unreadable_is_none(platform, data):
    assert graph_comment_age_exceeded(data, platform, NOW) is None
