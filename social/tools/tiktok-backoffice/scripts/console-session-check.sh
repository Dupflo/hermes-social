#!/usr/bin/env bash
# TikTok session check, run every 10 min by the hermes-console worker (job `tiktok-session-check`, s20).
#
# Called as `bash <this file>` in an exec detached inside hermes-social-hermes-1, as user `hermes`,
# under `flock` + `timeout` (hermes-console ADR 011 and 022, contract of the job: console ADR 049).
# It runs `app.session_check` in the console venv of tiktok-backoffice. The Camofox key is read by the
# module from the container environment (CAMOFOX_API_KEY, deploy/.env through env_file); the console
# injects nothing and this wrapper never prints the environment. The module prints the CONSOLE_RESULT
# line of ADR 011 itself, as the last line, and its exit code is propagated (11 = unexpected Camofox
# response, 12 = tabs saturated, 13 = internal error). It opens no database and never navigates a tab.
set -euo pipefail

export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/tiktok-backoffice}"

cd /opt/repos/hermes-social/social/tools/tiktok-backoffice

exec uv run --frozen --no-dev python -m app.session_check
