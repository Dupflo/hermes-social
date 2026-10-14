#!/usr/bin/env bash
# TikTok replies and DMs, run by the hermes-console worker (job `tiktok-reply`, s24, console ADR 055).
#
# Called as `bash <this file>` in an exec detached inside hermes-social-hermes-1, as user `hermes`, under `flock`
# (lock of the `tiktok-browser` group, shared with tiktok-session-check, tiktok-login and tiktok-scan) + `timeout`
# (hermes-console ADR 011 and 022). It runs `app.tiktok_reply` in the console venv of tiktok-backoffice, forwarding
# its arguments (`--check-only`: counts and guards only, no browser, no write besides the schema; `--max-items N`,
# 1-5, CLI only). The owner handle and the hourly limit (TIKTOK_OWNER_HANDLE, TIKTOK_REPLY_MAX_PER_HOUR) and the
# SadCaptcha key are read by the module from the exec environment; this wrapper never prints the environment.
# The module prints the CONSOLE_RESULT line of ADR 011 itself, as the last line, and its exit code is propagated
# (21 = browser unavailable or page unusable, 22 = captcha unsolved, 23 = internal error or SIGTERM,
# 24 = TikTok session lost, 25 = invalid configuration).
# This wrapper opens no database.
set -euo pipefail

export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/tiktok-backoffice}"

cd /opt/repos/hermes-social/social/tools/tiktok-backoffice

exec uv run --frozen --no-dev python -m app.tiktok_reply "$@"
