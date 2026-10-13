"""s22 — console-mouse Camofox plugin: mount, declaration and invariants (hermes-console ADR 053).

Read as text on purpose (no YAML dependency added), same pattern as test_deploy_camofox_key.py.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
DEPLOY = REPO / "deploy"
COMPOSE = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
CONFIG = json.loads((DEPLOY / "camofox.config.json").read_text(encoding="utf-8"))
PLUGIN_DIR = DEPLOY / "camofox-plugins" / "console-mouse"
MOUNT = "      - ./camofox-plugins/console-mouse:/app/plugins/console-mouse:ro"


def service_block(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", COMPOSE, re.M | re.S)
    assert match, name
    return match.group(1)


def test_camofox_mounts_the_plugin_sub_directory_only():
    block = service_block("camofox")
    assert MOUNT in block.splitlines()
    assert COMPOSE.count("camofox-plugins/") == 1


def test_nothing_is_mounted_on_the_bare_plugins_directory():
    for line in COMPOSE.splitlines():
        assert re.search(r":/app/plugins(:ro)?$", line.strip()) is None, line


def test_camofox_keeps_its_other_volumes_and_vnc():
    block = service_block("camofox")
    lines = block.splitlines()
    assert "      - ./camofox-data:/root/.camofox" in lines
    assert "      - ./camofox.config.json:/app/camofox.config.json:ro" in lines
    assert re.search(r'^      ENABLE_VNC: "1"$', block, re.M)


def test_config_adds_the_plugin_and_keeps_the_others():
    plugins = CONFIG["plugins"]
    assert plugins["youtube"] == {"enabled": True}
    assert plugins["persistence"] == {"enabled": True, "profileDir": "/root/.camofox/profiles", "indexedDB": True}
    assert plugins["vnc"] == {"enabled": False, "resolution": "1920x1080"}
    assert plugins["console-mouse"] == {"enabled": True}
    assert set(plugins) == {"youtube", "persistence", "vnc", "console-mouse"}


def test_plugin_directory_is_an_esm_package_without_dependency():
    assert (PLUGIN_DIR / "index.js").is_file()
    package = json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))
    assert package["type"] == "module"
    assert "dependencies" not in package
    assert sorted(p.name for p in PLUGIN_DIR.iterdir()) == ["index.js", "package.json"]


FORBIDDEN = [
    "tab:type",
    "events.on",
    "ctx.events",
    ".on(",
    ".once(",
    "addListener",
    "route(",
    "context.",
    "waitForEvent",
    "import ",
    "require(",
    "keyboard",
    "goto",
    "reload",
    "evaluate",
]


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_plugin_source_has_no_forbidden_pattern(pattern):
    source = (PLUGIN_DIR / "index.js").read_text(encoding="utf-8")
    assert pattern not in source


def test_plugin_goes_through_ctx_auth():
    source = (PLUGIN_DIR / "index.js").read_text(encoding="utf-8")
    assert "ctx.auth()" in source


def test_console_mouse_node_suite():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    result = subprocess.run(
        [node, "--test", "deploy/camofox-plugins/tests/*.test.mjs"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
