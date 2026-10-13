"""s17 (console ADR 045 §3) — `GraphClient.send_page_message`: POST /{page_id}/messages, RESPONSE, no tag."""

import json

import httpx
import pytest

from app.graph_client import GraphAPIError, GraphClient


def _body(request: httpx.Request):
    return json.loads(request.content.decode("utf-8"))


@pytest.mark.asyncio
async def test_send_page_message_posts_the_exact_response_body():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"recipient_id": "psid-1", "message_id": "m_1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        result = await client.send_page_message("999", "psid-1", "Bonjour 👋")

    assert result == {"recipient_id": "psid-1", "message_id": "m_1"}
    [request] = requests
    assert request.method == "POST"
    assert request.url.path == "/v21.0/999/messages"
    assert request.url.params["access_token"] == "page-token"
    assert set(request.url.params.keys()) == {"access_token"}
    body = _body(request)
    assert body == {"recipient": {"id": "psid-1"}, "messaging_type": "RESPONSE", "message": {"text": "Bonjour 👋"}}
    assert "tag" not in body


@pytest.mark.asyncio
async def test_send_page_message_outside_the_window_raises_the_meta_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "(#10) This message is sent outside of allowed window.",
                    "type": "OAuthException",
                    "code": 10,
                    "error_subcode": 2018278,
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        with pytest.raises(GraphAPIError) as caught:
            await client.send_page_message("999", "psid-1", "Bonjour")

    assert caught.value.code == 10
    assert caught.value.subcode == 2018278
    assert caught.value.http_status == 400
    assert caught.value.meta_type == "OAuthException"
