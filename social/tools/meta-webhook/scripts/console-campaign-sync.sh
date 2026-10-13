#!/usr/bin/env bash
# Kanban -> campaigns sync (Meta and TikTok), run every 15 min by the hermes-console worker (job `campaign-sync`).
#
# Called as `bash <this file>` in an exec detached inside hermes-social-hermes-1, as user `hermes`,
# under `flock` + `timeout` (hermes-console ADR 011 and 022, contract of the job: ADR 040). It runs the
# repo's `app.console_campaign_sync` with absolute paths: Meta in process, TikTok through
# `uv run tiktok-backoffice` in its own console venv. The relative decoy data/processed_comments.sqlite3
# of the project dir is never read nor written. The module prints the CONSOLE_RESULT line of ADR 011
# itself, as the last line; nothing is printed after it and its exit code is propagated
# (6 = Meta failed, 7 = TikTok failed, 8 = both failed, 9 = kanban missing, 10 = invalid configuration).
# No Hermes cron is replaced: the TikTok cron auto-DM worker still runs its own sync until s23.
set -euo pipefail

PROJECT="${PROJECT:-/opt/repos/hermes-social/social/tools/meta-webhook}"
TIKTOK_PROJECT="${TIKTOK_PROJECT:-/opt/repos/hermes-social/social/tools/tiktok-backoffice}"
KANBAN_DB="${KANBAN_DB:-/opt/data/kanban/boards/meta-campaigns/kanban.db}"
META_DB="${META_DB:-/opt/data/meta-comment-dm-automation/data/processed_comments.sqlite3}"
TIKTOK_DB="${TIKTOK_DB:-/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3}"
TIKTOK_VENV="${TIKTOK_VENV:-/opt/data/console-venvs/tiktok-backoffice}"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/opt/data/console-venvs/meta-webhook}"

cd "$PROJECT"

# Proof, per run, of which module is executed (same interpreter as below).
uv run --frozen --no-dev python -c 'import app.console_campaign_sync as m; print("console: module=" + m.__file__)'

exec uv run --frozen --no-dev python -m app.console_campaign_sync \
  --kanban-db "$KANBAN_DB" \
  --meta-db "$META_DB" \
  --tiktok-db "$TIKTOK_DB" \
  --tiktok-project "$TIKTOK_PROJECT" \
  --tiktok-venv "$TIKTOK_VENV"
