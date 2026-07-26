import httpx
import pytest

from app.graph_client import GraphClient, GraphAPIError


@pytest.mark.asyncio
async def test_reply_to_instagram_comment_posts_expected_payload():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "reply-id"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        result = await client.reply_to_instagram_comment("comment-1", "C'est envoyé")

    assert result == {"id": "reply-id"}
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/comment-1/replies")
    assert requests[0].url.params["access_token"] == "page-token"
    assert dict(requests[0].url.params)["message"] == "C'est envoyé"


@pytest.mark.asyncio
async def test_reply_to_facebook_comment_posts_expected_payload():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "reply-id"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        result = await client.reply_to_facebook_comment("comment-1", "C'est envoyé")

    assert result == {"id": "reply-id"}
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/comment-1/comments")
    assert requests[0].url.params["access_token"] == "page-token"
    assert dict(requests[0].url.params)["message"] == "C'est envoyé"


@pytest.mark.asyncio
async def test_private_reply_to_instagram_comment_posts_expected_payload():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"recipient_id": "ig-user", "message_id": "msg"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        result = await client.private_reply_to_instagram_comment(
            page_id="page-1",
            comment_id="comment-1",
            text="Voici la ressource",
        )

    assert result["message_id"] == "msg"
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/page-1/messages")
    assert requests[0].url.params["access_token"] == "page-token"
    assert request_body(requests[0]) == {
        "recipient": {"comment_id": "comment-1"},
        "message": {"text": "Voici la ressource"},
    }


@pytest.mark.asyncio
async def test_private_reply_to_facebook_comment_posts_expected_payload():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "message-id"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        result = await client.private_reply_to_facebook_comment("page-1", "comment-1", "Voici la ressource")

    assert result == {"id": "message-id"}
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/page-1/messages")
    assert requests[0].url.params["access_token"] == "page-token"
    assert request_body(requests[0]) == {
        "recipient": {"comment_id": "comment-1"},
        "message": {"text": "Voici la ressource"},
    }


@pytest.mark.asyncio
async def test_send_direct_message_posts_to_psid():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message_id": "dm-message-id"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        result = await client.send_direct_message("psid-1", "Bonjour")

    assert result == {"message_id": "dm-message-id"}
    assert requests[0].method == "POST"
    assert requests[0].url.path.endswith("/me/messages")
    assert requests[0].url.params["access_token"] == "page-token"
    assert request_body(requests[0]) == {
        "recipient": {"id": "psid-1"},
        "message": {"text": "Bonjour"},
    }


@pytest.mark.asyncio
async def test_graph_client_reads_facebook_private_reply_eligibility():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"can_reply_privately": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        result = await client.can_reply_privately_to_facebook_comment("comment-1")

    assert result is False
    assert requests[0].method == "GET"
    assert requests[0].url.path.endswith("/comment-1")
    assert requests[0].url.params["access_token"] == "page-token"
    assert dict(requests[0].url.params)["fields"] == "can_reply_privately"


@pytest.mark.asyncio
async def test_graph_client_raises_clear_error_for_meta_error_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Permissions error", "code": 200}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        with pytest.raises(GraphAPIError, match="Permissions error"):
            await client.like_comment("comment-1")


def request_body(request: httpx.Request) -> dict:
    return __import__("json").loads(request.content.decode())
