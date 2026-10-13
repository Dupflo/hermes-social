"""s09 — deploy/docker-compose.yml: launcher command and the console network (console ADR 030).

Read as text on purpose (no YAML dependency added).
"""

import re
from pathlib import Path


COMPOSE = (Path(__file__).resolve().parents[4] / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")


def service_block(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", COMPOSE, re.M | re.S)
    assert match, name
    return match.group(1)


def top_level_block(name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\n(.*?)(?=^\S|\Z)", COMPOSE, re.M | re.S)
    assert match, name
    return match.group(1)


def list_items(block: str, key: str) -> list[str]:
    match = re.search(rf"^    {key}:\n((?:      - .*\n?)+)", block, re.M)
    assert match, key
    return [line.strip()[2:].split("#")[0].strip().strip('"') for line in match.group(1).splitlines()]


def services() -> list[str]:
    return re.findall(r"^  ([a-z0-9-]+):\n", top_level_block("services"), re.M)


def test_meta_webhook_runs_the_launcher():
    block = service_block("meta-webhook")
    assert 'command: ["uv", "run", "python", "-m", "app.serve"]' in block
    assert "uvicorn" not in re.search(r"^    command: .*$", block, re.M).group(0)


def test_meta_webhook_publishes_only_the_public_port():
    assert list_items(service_block("meta-webhook"), "ports") == ["127.0.0.1:8791:8791"]
    assert not re.search(r"8792\s*:|:\s*8792\b", COMPOSE)


def test_console_network_is_internal_and_joined_by_meta_webhook_only():
    networks = top_level_block("networks")
    console = re.search(r"^  console:\n((?:    .*\n?)+)", networks, re.M)
    assert console
    assert "name: hermes-social-console" in console.group(1)
    assert "internal: true" in console.group(1)

    assert list_items(service_block("meta-webhook"), "networks") == ["default", "console"]
    for name in services():
        if name != "meta-webhook":
            assert "networks:" not in service_block(name), name


def test_no_internal_token_in_the_compose():
    assert "CONSOLE_INTERNAL_TOKEN" not in COMPOSE
