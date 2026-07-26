#!/usr/bin/env bash
set -euo pipefail

cd /opt/data/meta-comment-dm-automation
UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/meta-comment-dm-automation-token-renewal-venv}" \
  uv run python -m app.token_renewer \
  --env-file .env \
  --deploy-trigger .deploy-trigger
