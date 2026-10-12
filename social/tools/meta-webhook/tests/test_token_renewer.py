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


# --- s04-meta-token-renewal: private files, owner kept, network errors, no deploy trigger ---

import os
import stat as stat_mod

from app import token_renewer
from app.token_renewer import main, write_env_file


def _mode(path: Path) -> int:
    return stat_mod.S_IMODE(os.stat(path).st_mode)


def _fake_graph_client(monkeypatch, handler):
    real_httpx_client = httpx.Client

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self._client = real_httpx_client(transport=httpx.MockTransport(handler))

        def __enter__(self):
            return self._client.__enter__()

        def __exit__(self, *args):
            return self._client.__exit__(*args)

    monkeypatch.setattr("app.token_renewer.httpx.Client", FakeClient)


def _write_minimal_env(env_path: Path, extra: list[str] | None = None) -> None:
    env_path.write_text(
        "\n".join(
            [
                "META_APP_ID=app-1",
                "META_APP_SECRET=secret",
                "META_USER_ACCESS_TOKEN=old-user-token",
                "META_PAGE_ACCESS_TOKEN=old-page-token",
                "META_PAGE_ID=page-1",
                *(extra or []),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_write_env_file_makes_a_preexisting_world_readable_backup_private(tmp_path):
    previous = os.umask(0o022)
    try:
        env_path = tmp_path / ".env"
        env_path.write_text("A=1\n", encoding="utf-8")
        backup = tmp_path / ".env.bak"
        backup.write_text("old\n", encoding="utf-8")
        os.chmod(backup, 0o644)

        write_env_file(env_path, ["A=2"])

        assert _mode(backup) == 0o600
        assert backup.read_text(encoding="utf-8") == "A=1\n"
    finally:
        os.umask(previous)


def test_write_env_file_leaves_env_and_backup_in_0600_under_a_permissive_umask(tmp_path):
    previous = os.umask(0o022)
    try:
        env_path = tmp_path / ".env"
        env_path.write_text("A=1\n", encoding="utf-8")
        os.chmod(env_path, 0o600)

        write_env_file(env_path, ["A=2"])

        assert _mode(env_path) == 0o600
        assert _mode(tmp_path / ".env.bak") == 0o600
        assert env_path.read_text(encoding="utf-8") == "A=2\n"
        assert not (tmp_path / ".env.tmp").exists()
    finally:
        os.umask(previous)


def test_write_env_file_keeps_the_original_owner(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n", encoding="utf-8")
    before = os.stat(env_path)

    write_env_file(env_path, ["A=2"])

    after = os.stat(env_path)
    backup = os.stat(tmp_path / ".env.bak")
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert (backup.st_uid, backup.st_gid) == (before.st_uid, before.st_gid)


def test_write_env_file_chowns_new_files_to_the_original_owner_when_it_differs(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n", encoding="utf-8")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(token_renewer, "_owner_of", lambda path: (10000, 10000))
    monkeypatch.setattr(token_renewer.os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))

    write_env_file(env_path, ["A=2"])

    # .bak then .tmp (which becomes the .env)
    assert calls == [(10000, 10000), (10000, 10000)]


def test_write_env_file_does_not_chown_when_the_owner_already_matches(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n", encoding="utf-8")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(token_renewer.os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))

    write_env_file(env_path, ["A=2"])

    assert calls == []


def test_write_env_file_failure_leaves_env_untouched_and_no_tmp(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("A=1\n", encoding="utf-8")
    monkeypatch.setattr(token_renewer, "_owner_of", lambda path: (10000, 10000))
    seen = {"n": 0}

    def flaky_fchown(fd, uid, gid):
        seen["n"] += 1
        if seen["n"] == 2:  # the .tmp, after the backup succeeded
            raise PermissionError("operation not permitted")

    monkeypatch.setattr(token_renewer.os, "fchown", flaky_fchown)

    with pytest.raises(PermissionError):
        write_env_file(env_path, ["A=2"])

    assert env_path.read_text(encoding="utf-8") == "A=1\n"
    assert not (tmp_path / ".env.tmp").exists()


def test_main_reports_a_network_error_in_one_line_without_traceback(monkeypatch, tmp_path, capsys):
    env_path = tmp_path / ".env"
    _write_minimal_env(env_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _fake_graph_client(monkeypatch, handler)

    code = main(["--env-file", str(env_path), "--no-deploy-trigger"])

    out = capsys.readouterr()
    assert code == 1
    assert out.out.strip() == "Meta token renewal failed: network error (ConnectError)"
    assert "Traceback" not in out.out + out.err
    assert env_path.read_text(encoding="utf-8").count("old-user-token") == 1


def test_main_no_deploy_trigger_ignores_the_env_trigger_key(monkeypatch, tmp_path, capsys):
    env_path = tmp_path / ".env"
    trigger = tmp_path / ".deploy-trigger"
    _write_minimal_env(env_path, [f"META_TOKEN_RENEWAL_DEPLOY_TRIGGER={trigger}"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "new-user-token"})
        return httpx.Response(200, json={"data": [{"id": "page-1", "access_token": "new-page-token"}]})

    _fake_graph_client(monkeypatch, handler)

    code = main(["--env-file", str(env_path), "--no-deploy-trigger"])

    assert code == 0
    assert not trigger.exists()
    assert read_env_file(env_path).values["META_USER_ACCESS_TOKEN"] == "new-user-token"
