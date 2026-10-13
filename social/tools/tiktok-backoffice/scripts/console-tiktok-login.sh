#!/usr/bin/env bash
# TikTok login, run by the hermes-console worker (job `tiktok-login`, s21, console ADR 050/051).
#
# Called as `bash <this file>` in an exec detached inside hermes-social-hermes-1, as user `hermes`,
# under `flock` (lock of the `tiktok-browser` group) + `timeout` (hermes-console ADR 011 and 022).
# It runs `app.tiktok_login` in the console venv of tiktok-backoffice, forwarding its arguments
# (`--check-only`: read the session cookies only, never navigate nor type). The credentials are read by
# the module from the exec environment (vault values injected by the console, else deploy/.env); this
# wrapper never prints the environment. The module prints the CONSOLE_RESULT line of ADR 011 itself, as
# the last line, and its exit code is propagated (14 = unexpected Camofox response or deadline, 15 = tabs
# saturated, 16 = internal error). It opens no database.
set -euo pipefail

export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/tiktok-backoffice}"

cd /opt/repos/hermes-social/social/tools/tiktok-backoffice

exec uv run --frozen --no-dev python -m app.tiktok_login "$@"
