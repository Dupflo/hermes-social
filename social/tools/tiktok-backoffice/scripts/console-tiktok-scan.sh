#!/usr/bin/env bash
# TikTok comment scan, run by the hermes-console worker (job `tiktok-scan`, s23, console ADR 054).
#
# Called as `bash <this file>` in an exec detached inside hermes-social-hermes-1, as user `hermes`, under `flock`
# (lock of the `tiktok-browser` group, shared with tiktok-session-check and tiktok-login) + `timeout`
# (hermes-console ADR 011 and 022). It runs `app.tiktok_scan` in the console venv of tiktok-backoffice, forwarding its
# arguments (`--check-only`: counts and guards only, no browser, no processor). The versioned review processor runs
# with the container's own `python3`, like the legacy cron: it is resolved here, before `uv run` puts the venv first
# on PATH. The SadCaptcha key is read by the module from the exec environment; this wrapper never prints the
# environment. The module prints the CONSOLE_RESULT line of ADR 011 itself, as the last line, and its exit code is
# propagated (17 = no video read, 18 = captcha unsolved, 19 = internal error, 20 = TikTok session lost).
# This wrapper opens no database.
set -euo pipefail

export SCAN_PROCESSOR_PYTHON="$(command -v python3)"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/tiktok-backoffice}"

cd /opt/repos/hermes-social/social/tools/tiktok-backoffice

exec uv run --frozen --no-dev python -m app.tiktok_scan "$@"
