from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

import httpx


class TokenRenewalError(RuntimeError):
    """Raised when Meta token renewal cannot be completed."""


@dataclass(frozen=True)
class EnvFile:
    values: dict[str, str]
    lines: list[str]


def read_env_file(path: str | Path) -> EnvFile:
    env_path = Path(path)
    lines = env_path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return EnvFile(values=values, lines=lines)


def update_env_lines(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = dict(updates)
    updated_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated_lines.append(line)
            continue

        key, _ = line.split("=", 1)
        key = key.strip()
        if key in remaining:
            updated_lines.append(f"{key}={remaining.pop(key)}")
        else:
            updated_lines.append(line)

    for key, value in remaining.items():
        updated_lines.append(f"{key}={value}")

    return updated_lines


def _owner_of(path: Path) -> tuple[int, int] | None:
    """uid/gid of an existing file, None when it does not exist."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return st.st_uid, st.st_gid


def _write_private(path: Path, data: str, owner: tuple[int, int] | None) -> None:
    """Write `data` to `path` in 0600 from the very first byte, owned by `owner`.

    `os.open(..., 0o600)` only applies to a new file, hence the `fchmod` for a
    pre-existing one (e.g. an old `.bak` left in 0644). The owner of the
    original `.env` is kept when the process runs as another user (root exec).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
        if owner is not None:
            current = os.fstat(fd)
            if (current.st_uid, current.st_gid) != owner:
                os.fchown(fd, owner[0], owner[1])
    finally:
        os.close(fd)


def write_env_file(path: str | Path, lines: list[str]) -> None:
    env_path = Path(path)
    backup_path = env_path.with_suffix(env_path.suffix + ".bak")
    tmp_path = env_path.with_suffix(env_path.suffix + ".tmp")
    owner = _owner_of(env_path)

    if env_path.exists():
        _write_private(backup_path, env_path.read_text(encoding="utf-8"), owner)

    try:
        _write_private(tmp_path, "\n".join(lines) + "\n", owner)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, env_path)
    env_path.chmod(0o600)


def require_env(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        raise TokenRenewalError(f"Missing required environment value: {key}")
    return value


def graph_get(client: httpx.Client, url: str, params: dict[str, str]) -> dict:
    response = client.get(url, params=params)
    data = response.json() if response.content else {}
    if response.is_error:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("message") or response.text
        code = error.get("code")
        raise TokenRenewalError(f"Meta Graph API error {code}: {message}")
    if not isinstance(data, dict):
        raise TokenRenewalError("Meta Graph API returned an unexpected response")
    return data


def exchange_user_token(
    *,
    client: httpx.Client,
    graph_api_version: str,
    app_id: str,
    app_secret: str,
    user_access_token: str,
) -> str:
    data = graph_get(
        client,
        f"https://graph.facebook.com/{graph_api_version}/oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": user_access_token,
        },
    )
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise TokenRenewalError("Meta did not return a renewed user access token")
    return token


def fetch_page_access_token(
    *,
    client: httpx.Client,
    graph_api_version: str,
    user_access_token: str,
    page_id: str,
) -> str:
    data = graph_get(
        client,
        f"https://graph.facebook.com/{graph_api_version}/me/accounts",
        {"access_token": user_access_token, "fields": "id,name,access_token"},
    )
    pages = data.get("data", [])
    if not isinstance(pages, list):
        raise TokenRenewalError("Meta /me/accounts returned an unexpected response")

    for page in pages:
        if isinstance(page, dict) and str(page.get("id")) == page_id:
            token = page.get("access_token")
            if isinstance(token, str) and token:
                return token
            raise TokenRenewalError("Matching Page was returned without an access token")

    raise TokenRenewalError("Configured META_PAGE_ID was not found in /me/accounts")


def renew_tokens(
    env_path: str | Path = ".env",
    deploy_trigger_path: str | Path | None | bool = None,
) -> str:
    """Renew the tokens and rewrite the .env.

    `deploy_trigger_path=False` disables the trigger entirely (neither the
    argument nor the `META_TOKEN_RENEWAL_DEPLOY_TRIGGER` key of the .env).
    """
    env_file = read_env_file(env_path)
    values = env_file.values
    graph_api_version = values.get("GRAPH_API_VERSION") or values.get("META_GRAPH_API_VERSION") or "v21.0"

    app_id = require_env(values, "META_APP_ID")
    app_secret = require_env(values, "META_APP_SECRET")
    user_access_token = require_env(values, "META_USER_ACCESS_TOKEN")
    page_id = require_env(values, "META_PAGE_ID")

    with httpx.Client(timeout=30) as client:
        new_user_token = exchange_user_token(
            client=client,
            graph_api_version=graph_api_version,
            app_id=app_id,
            app_secret=app_secret,
            user_access_token=user_access_token,
        )
        new_page_token = fetch_page_access_token(
            client=client,
            graph_api_version=graph_api_version,
            user_access_token=new_user_token,
            page_id=page_id,
        )

    new_lines = update_env_lines(
        env_file.lines,
        {
            "META_USER_ACCESS_TOKEN": new_user_token,
            "META_PAGE_ACCESS_TOKEN": new_page_token,
        },
    )
    write_env_file(env_path, new_lines)

    if deploy_trigger_path is False:
        trigger = None
    else:
        trigger = deploy_trigger_path or values.get("META_TOKEN_RENEWAL_DEPLOY_TRIGGER")
    if trigger:
        Path(trigger).touch()

    return "Meta tokens renewed and .env updated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Renew Meta long-lived user token and Page token.")
    parser.add_argument("--env-file", default=".env", help="Path to the .env file to update")
    parser.add_argument(
        "--deploy-trigger",
        default=None,
        help="Optional file to touch after updating .env, for deployment/restart automation",
    )
    parser.add_argument(
        "--no-deploy-trigger",
        action="store_true",
        help="Never touch a deploy trigger (neither --deploy-trigger nor META_TOKEN_RENEWAL_DEPLOY_TRIGGER)",
    )
    args = parser.parse_args(argv)

    trigger: str | None | bool = False if args.no_deploy_trigger else args.deploy_trigger
    try:
        message = renew_tokens(args.env_file, trigger)
    except TokenRenewalError as exc:
        print(f"Meta token renewal failed: {exc}")
        return 1
    except httpx.HTTPError as exc:
        # No traceback: an httpx trace may carry the URL and its query (tokens).
        print(f"Meta token renewal failed: network error ({type(exc).__name__})")
        return 1

    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
