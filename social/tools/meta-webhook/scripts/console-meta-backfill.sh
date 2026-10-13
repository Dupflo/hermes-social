#!/usr/bin/env bash
# Meta verification/backfill worker, run every 15 min by the hermes-console worker (job `meta-backfill`).
#
# Called as `bash <this file>` in an exec detached inside hermes-social-hermes-1, as user `hermes`,
# under `flock` + `timeout` (hermes-console ADR 011 and 022). It runs the repo's `app.meta_cron_verify`
# with absolute --env-file and --db: the empty .env and data/processed_comments.sqlite3 of the project
# dir are decoys and are never read nor written. The module prints the CONSOLE_RESULT line of ADR 011
# itself, as the last line; nothing is printed after it and its exit code is propagated
# (3 = Graph scan failed, 4 = invalid configuration). Exit 5 = the Hermes cron copy is still running.
set -euo pipefail

PROJECT="${PROJECT:-/opt/repos/hermes-social/social/tools/meta-webhook}"
META_ENV_FILE="${META_ENV_FILE:-/opt/data/meta-comment-dm-automation/.env}"
META_DB="${META_DB:-/opt/data/meta-comment-dm-automation/data/processed_comments.sqlite3}"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/meta-webhook}"

# Never run alongside the Hermes cron (3fc89601018c) until it is paused: two processes would send twice.
if pgrep -f /opt/data/scripts/meta_cron_verify.py >/dev/null; then
  echo "console: hermes_cron_running"
  exit 5
fi

cd "$PROJECT"

# Proof, per run, of which module is executed (same interpreter as below).
uv run --frozen --no-dev python -c 'import app.meta_cron_verify as m; print("console: module=" + m.__file__)'

exec uv run --frozen --no-dev python -m app.meta_cron_verify \
  --env-file "$META_ENV_FILE" \
  --db "$META_DB"
