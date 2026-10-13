"""s09 — app/serve.py runs the public app (:8791) and the internal app (:8792) in one process.

Real process: one SIGTERM stops both, and if one listener cannot start the other is
stopped too and the process exits non-zero (never :8791 alone without saying so).
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


TOOL_DIR = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start(tmp_path, public_port, internal_port):
    env = {
        **os.environ,
        "META_PUBLIC_PORT": str(public_port),
        "META_INTERNAL_PORT": str(internal_port),
        "PROCESSED_COMMENTS_DATABASE": str(tmp_path / "processed.sqlite3"),
        "CONSOLE_INTERNAL_TOKEN": "serve-test-token",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "app.serve"],
        cwd=TOOL_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def wait_ready(process, port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(process.stdout.read().decode(errors="replace"))
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("server did not start")


def wait_internal(port, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            return
        time.sleep(0.1)
    raise AssertionError("internal listener did not start")


def stop(process):
    if process.poll() is None:
        process.kill()
        process.wait(5)


def test_both_listeners_and_sigterm_stops_both(tmp_path):
    public_port, internal_port = free_port(), free_port()
    process = start(tmp_path, public_port, internal_port)
    try:
        wait_ready(process, public_port)
        wait_internal(internal_port)

        assert httpx.get(f"http://127.0.0.1:{public_port}/health").status_code == 200
        assert httpx.get(f"http://127.0.0.1:{public_port}/internal/health").status_code == 404
        assert httpx.get(f"http://127.0.0.1:{internal_port}/internal/health").status_code == 401
        assert (
            httpx.get(
                f"http://127.0.0.1:{internal_port}/internal/health",
                headers={"Authorization": "Bearer serve-test-token"},
            ).status_code
            == 200
        )

        process.send_signal(signal.SIGTERM)
        assert process.wait(10) == 0
        assert not port_open(public_port)
        assert not port_open(internal_port)
    finally:
        stop(process)


def test_internal_port_taken_stops_everything_with_an_error(tmp_path):
    public_port, internal_port = free_port(), free_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    holder.bind(("0.0.0.0", internal_port))
    holder.listen(1)
    process = start(tmp_path, public_port, internal_port)
    try:
        code = process.wait(10)
        output = process.stdout.read().decode(errors="replace")
        assert code != 0
        assert "serve_failed internal" in output
        assert not port_open(public_port)
    finally:
        stop(process)
        holder.close()
