"""s20: Camofox client (Bearer key, env config, typed errors) against a local HTTP server."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.camofox_reader import (
    DEFAULT_CAMOFOX_BASE_URL,
    DEFAULT_CAMOFOX_USER_ID,
    CamofoxClient,
    CamofoxHTTPError,
    CamofoxUnreachable,
)


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        # path (without query) -> (status, body dict)
        self.responses: dict[tuple[str, str], tuple[int, dict]] = {}


def _make_handler(recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):  # silence the test output
            return

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            recorder.requests.append({
                "method": method,
                "path": parts.path,
                "query": parse_qs(parts.query),
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw) if raw else None,
            })
            status, body = recorder.responses.get((method, parts.path), (200, {"ok": True}))
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


@pytest.fixture()
def server():
    recorder = _Recorder()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(recorder))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield recorder, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_bearer_header_sent_only_when_key_set(server):
    recorder, base = server
    recorder.responses[("GET", "/health")] = (200, {"ok": True})
    CamofoxClient(base_url=base, api_key="k-123").health()
    CamofoxClient(base_url=base).health()
    assert recorder.requests[0]["headers"].get("authorization") == "Bearer k-123"
    assert "authorization" not in recorder.requests[1]["headers"]


def test_from_env_defaults_and_overrides():
    default = CamofoxClient.from_env({})
    assert default.base_url == DEFAULT_CAMOFOX_BASE_URL
    assert default.user_id == DEFAULT_CAMOFOX_USER_ID == "hermes_80317d7dba"
    assert default.api_key == ""
    custom = CamofoxClient.from_env({
        "CAMOFOX_URL": "http://other:1",
        "CAMOFOX_USER_ID": "hermes_x",
        "CAMOFOX_API_KEY": "  secret-key \n",
    })
    assert custom.base_url == "http://other:1"
    assert custom.user_id == "hermes_x"
    assert custom.api_key == "secret-key"


def test_repr_does_not_leak_key():
    client = CamofoxClient(api_key="SECRETAPIKEY")
    assert "SECRETAPIKEY" not in repr(client)
    assert "SECRETAPIKEY" not in str(client)


def test_list_tabs_sends_user_id_in_query(server):
    recorder, base = server
    recorder.responses[("GET", "/tabs")] = (
        200,
        {"running": True, "tabs": [{"tabId": "t1", "url": "https://www.tiktok.com/", "title": "x", "listItemId": "l"}]},
    )
    tabs = CamofoxClient(base_url=base, user_id="hermes_u").list_tabs()
    assert tabs == [{"tabId": "t1", "url": "https://www.tiktok.com/"}]
    assert recorder.requests[0]["query"] == {"userId": ["hermes_u"]}


def test_delete_tab_uses_query_user_id(server):
    recorder, base = server
    CamofoxClient(base_url=base, user_id="hermes_u").delete_tab("tab-9")
    req = recorder.requests[0]
    assert req["method"] == "DELETE"
    assert req["path"] == "/tabs/tab-9"
    assert req["query"] == {"userId": ["hermes_u"]}


def test_create_blank_tab_has_no_url(server):
    recorder, base = server
    recorder.responses[("POST", "/tabs")] = (200, {"tabId": "new-1"})
    tab_id = CamofoxClient(base_url=base, user_id="hermes_u").create_blank_tab("console-session-check")
    assert tab_id == "new-1"
    body = recorder.requests[0]["body"]
    assert body == {"userId": "hermes_u", "sessionKey": "console-session-check"}
    assert "url" not in body


def test_storage_state_path(server):
    recorder, base = server
    recorder.responses[("GET", "/sessions/hermes_u/storage_state")] = (200, {"cookies": [], "origins": []})
    state = CamofoxClient(base_url=base, user_id="hermes_u", api_key="k").storage_state()
    assert state == {"cookies": [], "origins": []}
    assert recorder.requests[0]["method"] == "GET"
    assert recorder.requests[0]["headers"]["authorization"] == "Bearer k"


def test_http_error_is_typed_without_body_or_key(server):
    recorder, base = server
    recorder.responses[("GET", "/sessions/hermes_u/storage_state")] = (
        403,
        {"error": "Forbidden BODYMARKER sessionid=SECRETCOOKIEVALUE"},
    )
    client = CamofoxClient(base_url=base, user_id="hermes_u", api_key="SECRETAPIKEY")
    with pytest.raises(CamofoxHTTPError) as info:
        client.storage_state()
    assert info.value.status == 403
    text = str(info.value) + repr(info.value)
    for leaked in ("BODYMARKER", "SECRETCOOKIEVALUE", "SECRETAPIKEY", base, "storage_state"):
        assert leaked not in text


def test_connection_refused_is_unreachable():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(CamofoxUnreachable):
        CamofoxClient(base_url=f"http://127.0.0.1:{port}").health()


def test_fetch_comments_reads_env(monkeypatch):
    from app import camofox_reader

    def fake_from_env(environ=None):
        return CamofoxClient(base_url="http://env:1", user_id="hermes_env", api_key="envkey")

    captured = []

    def spy_create(self, **kwargs):
        captured.append((self.base_url, self.user_id, self.api_key))
        raise CamofoxUnreachable()

    monkeypatch.setattr(camofox_reader.CamofoxClient, "from_env", staticmethod(fake_from_env))
    monkeypatch.setattr(camofox_reader.CamofoxClient, "create_tab", spy_create)
    result = camofox_reader.fetch_comments_from_camofox(video_url="https://www.tiktok.com/@a/video/1")
    assert result["error"] == "camofox_unreachable"
    camofox_reader.fetch_comments_from_camofox(
        video_url="https://www.tiktok.com/@a/video/1", base_url="http://explicit:2", user_id="hermes_explicit"
    )
    assert captured == [("http://env:1", "hermes_env", "envkey"), ("http://explicit:2", "hermes_explicit", "envkey")]


def test_cli_fetch_defaults_come_from_env(monkeypatch, tmp_path):
    from app import cli

    seen = {}

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return {"ok": False, "error": "x", "comments": []}

    monkeypatch.setattr(cli, "fetch_comments_from_camofox", fake_fetch)
    cli.run(["--db", str(tmp_path / "t.sqlite3"), "fetch-comments-camofox", "--video-url", "https://www.tiktok.com/@a/video/1"])
    assert seen["base_url"] is None and seen["user_id"] is None
    cli.run(["--db", str(tmp_path / "t.sqlite3"), "fetch-comments-camofox", "--video-url", "https://www.tiktok.com/@a/video/1", "--base-url", "http://b:1", "--user-id", "hermes_b"])
    assert seen["base_url"] == "http://b:1" and seen["user_id"] == "hermes_b"
