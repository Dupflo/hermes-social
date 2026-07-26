from pathlib import Path

import httpx
import pytest

from app.token_renewer import (
    TokenRenewalError,
    exchange_user_token,
    fetch_page_access_token,
    read_env_file,
    renew_tokens,
    update_env_lines,
)


def test_update_env_lines_preserves_comments_and_updates_existing_values():
    lines = [
        "# Tokens",
        "META_USER_ACCESS_TOKEN=old-user",
        "META_PAGE_ACCESS_TOKEN=old-page",
        "RESOURCE_KEYWORD=proxy",
    ]

    updated = update_env_lines(
        lines,
        {
            "META_USER_ACCESS_TOKEN": "new-user",
            "META_PAGE_ACCESS_TOKEN": "new-page",
        },
    )

    assert updated == [
        "# Tokens",
        "META_USER_ACCESS_TOKEN=new-user",
        "META_PAGE_ACCESS_TOKEN=new-page",
        "RESOURCE_KEYWORD=proxy",
    ]


def test_update_env_lines_appends_missing_values():
    updated = update_env_lines(["META_APP_ID=app-1"], {"META_PAGE_ACCESS_TOKEN": "page-token"})

    assert updated == ["META_APP_ID=app-1", "META_PAGE_ACCESS_TOKEN=page-token"]


def test_exchange_user_token_returns_access_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/oauth/access_token"
        assert request.url.params["grant_type"] == "fb_exchange_token"
        assert request.url.params["client_id"] == "app-1"
        assert request.url.params["client_secret"] == "secret"
        assert request.url.params["fb_exchange_token"] == "old-user-token"
        return httpx.Response(200, json={"access_token": "new-user-token"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = exchange_user_token(
            client=client,
            graph_api_version="v21.0",
            app_id="app-1",
            app_secret="secret",
            user_access_token="old-user-token",
        )

    assert token == "new-user-token"


def test_fetch_page_access_token_returns_matching_page_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v21.0/me/accounts"
        assert request.url.params["access_token"] == "new-user-token"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "other-page", "access_token": "other-token"},
                    {"id": "page-1", "access_token": "new-page-token"},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        token = fetch_page_access_token(
            client=client,
            graph_api_version="v21.0",
            user_access_token="new-user-token",
            page_id="page-1",
        )

    assert token == "new-page-token"


def test_fetch_page_access_token_fails_when_page_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TokenRenewalError, match="META_PAGE_ID"):
            fetch_page_access_token(
                client=client,
                graph_api_version="v21.0",
                user_access_token="new-user-token",
                page_id="page-1",
            )


def test_renew_tokens_updates_env_file_and_touches_deploy_trigger(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    deploy_trigger = tmp_path / ".deploy-trigger"
    env_path.write_text(
        "\n".join(
            [
                "META_APP_ID=app-1",
                "META_APP_SECRET=secret",
                "META_USER_ACCESS_TOKEN=old-user-token",
                "META_PAGE_ACCESS_TOKEN=old-page-token",
                "META_PAGE_ID=page-1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "new-user-token"})
        if request.url.path.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": [{"id": "page-1", "access_token": "new-page-token"}]})
        raise AssertionError(f"unexpected request: {request.url}")

    real_httpx_client = httpx.Client

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self._client = real_httpx_client(transport=httpx.MockTransport(handler))

        def __enter__(self):
            return self._client.__enter__()

        def __exit__(self, *args):
            return self._client.__exit__(*args)

    monkeypatch.setattr("app.token_renewer.httpx.Client", FakeClient)

    message = renew_tokens(env_path, deploy_trigger)

    env = read_env_file(env_path)
    assert message == "Meta tokens renewed and .env updated"
    assert env.values["META_USER_ACCESS_TOKEN"] == "new-user-token"
    assert env.values["META_PAGE_ACCESS_TOKEN"] == "new-page-token"
    assert deploy_trigger.exists()
    assert Path(str(env_path) + ".bak").exists()
