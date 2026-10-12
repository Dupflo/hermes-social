#!/usr/bin/env bash
# Meta token renewal, run by the hermes-console worker (job `meta-token-renewal`).
#
# Called as `bash <this file>` in an exec detached inside hermes-social-hermes-1,
# as user `hermes`, under `flock` + `timeout` (hermes-console ADR 011 and 022).
# It runs the repo's `app.token_renewer` (never the frozen copy under
# /opt/data/meta-comment-dm-automation/app) and restarts nothing: the worker
# restarts meta-webhook itself after a successful exit. The last line is the
# CONSOLE_RESULT contract of ADR 011, printed on success only.
set -euo pipefail

PROJECT="${PROJECT:-/opt/repos/hermes-social/social/tools/meta-webhook}"
META_ENV_FILE="${META_ENV_FILE:-/opt/data/meta-comment-dm-automation/.env}"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/meta-webhook}"

cd "$PROJECT"

# Proof, per run, of which module is executed (same interpreter as below).
uv run --frozen --no-dev python -c 'import app.token_renewer as m; print("console: module=" + m.__file__)'

uv run --frozen --no-dev python -m app.token_renewer \
  --env-file "$META_ENV_FILE" \
  --no-deploy-trigger

echo "tokens renouvelés"
echo 'CONSOLE_RESULT {"status":"ok","reason":"tokens_renewed"}'
