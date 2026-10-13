"""s16 (console ADR 044) — Graph read methods for the console inbox.

httpx.MockTransport only: no network. The page token stays in the query like every existing call.
"""

import httpx
import pytest

from app.graph_client import CONVERSATION_FIELDS, MESSAGE_FIELDS, GraphAPIError, GraphClient


def recording(responses=None):
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if responses is not None:
            return responses(request)
        return httpx.Response(200, json={"data": []})

    return requests, httpx.MockTransport(handler)


def test_field_constants_are_the_adr_044_forms():
    assert CONVERSATION_FIELDS == "id,updated_time,participants,messages.limit(1){id,message,from,created_time}"
    assert MESSAGE_FIELDS == "id,message,from,created_time"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["instagram", "messenger"])
async def test_list_conversations_reads_the_page_conversations(platform):
    requests, transport = recording()
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        assert await client.list_conversations("999", platform, limit=25) == {"data": []}

    [request] = requests
    assert request.method == "GET"
    assert request.url.path == "/v21.0/999/conversations"
    params = dict(request.url.params)
    assert params == {
        "access_token": "page-token",
        "platform": platform,
        "fields": CONVERSATION_FIELDS,
        "limit": "25",
    }
    assert "after" not in params


@pytest.mark.asyncio
async def test_list_conversations_passes_after_when_given():
    requests, transport = recording()
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        await client.list_conversations("999", "messenger", limit=50, after="CURSOR-1")

    params = dict(requests[0].url.params)
    assert params["after"] == "CURSOR-1"
    assert params["limit"] == "50"


@pytest.mark.asyncio
async def test_get_conversation_participants_reads_the_conversation_node():
    requests, transport = recording()
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        await client.get_conversation_participants("t_123")

    [request] = requests
    assert request.method == "GET"
    assert request.url.path == "/v21.0/t_123"
    assert dict(request.url.params) == {"access_token": "page-token", "fields": "participants"}


@pytest.mark.asyncio
async def test_list_conversation_messages_reads_the_messages_edge():
    requests, transport = recording()
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        await client.list_conversation_messages("t_123", limit=20)
        await client.list_conversation_messages("t_123", limit=20, after="OLDER")

    first, second = requests
    assert first.method == "GET"
    assert first.url.path == "/v21.0/t_123/messages"
    assert dict(first.url.params) == {"access_token": "page-token", "fields": MESSAGE_FIELDS, "limit": "20"}
    assert dict(second.url.params)["after"] == "OLDER"


@pytest.mark.asyncio
async def test_a_meta_error_carries_its_type():
    _, transport = recording(
        lambda request: httpx.Response(
            400, json={"error": {"message": "(#3) x", "type": "OAuthException", "code": 3}}
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        with pytest.raises(GraphAPIError) as caught:
            await client.list_conversations("999", "instagram", limit=25)

    error = caught.value
    assert error.code == 3
    assert error.meta_type == "OAuthException"
    assert error.meta_message == "(#3) x"
    assert error.http_status == 400


@pytest.mark.asyncio
async def test_an_html_error_page_has_neither_code_nor_type():
    _, transport = recording(lambda request: httpx.Response(502, text="<html>bad gateway</html>"))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        with pytest.raises(GraphAPIError) as caught:
            await client.list_conversation_messages("t_123", limit=20)

    assert caught.value.code is None
    assert caught.value.meta_type is None
    assert caught.value.http_status == 502


def test_meta_type_defaults_to_none():
    assert GraphAPIError("x").meta_type is None
