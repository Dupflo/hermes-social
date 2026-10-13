"""s20 — deploy/docker-compose.yml: the Camofox API key reaches camofox by interpolation (console ADR 049).

Read as text on purpose (no YAML dependency added), same pattern as meta-webhook/tests/test_deploy_compose.py.
"""

import re
from pathlib import Path

COMPOSE = (Path(__file__).resolve().parents[4] / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")


def service_block(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", COMPOSE, re.M | re.S)
    assert match, name
    return match.group(1)


def test_camofox_receives_the_key_by_interpolation():
    block = service_block("camofox")
    environment = re.search(r"^    environment:\n((?:      .*\n?)+)", block, re.M)
    assert environment, "camofox environment"
    assert re.search(r"^      CAMOFOX_API_KEY: \$\{CAMOFOX_API_KEY:-\}$", environment.group(1), re.M)


def test_no_literal_key_value_anywhere():
    # Anchored on the YAML key: the bare pattern would match the `${CAMOFOX_API_KEY:-}` interpolation itself.
    assert re.search(r"^\s*CAMOFOX_API_KEY:[ \t]*[^$\s]", COMPOSE, re.M) is None
    assert re.search(r"CAMOFOX_API_KEY=", COMPOSE) is None


def test_hermes_keeps_its_env_file():
    block = service_block("hermes")
    assert re.search(r"^    env_file:\n      - \.env$", block, re.M)
