# Secrets and Runtime State

This repository is intended to be open-source friendly. Code, docs, templates, and tests can be committed; secrets and runtime state must stay outside git.

## Commit these

```text
README.md
docs/
.env.example
deploy/.env.example
docker-compose.yml
source code
tests
schemas/migrations
sanitized runbooks
```

## Never commit these

```text
.env
*.env except .env.example files
access tokens
refresh tokens
client secrets
app secrets
Git credentials
TikTok cookies
storage-state.json
Camofox/Firefox profiles
SQLite production databases
logs
screenshots/evidence from real users
browser cache/session artifacts
customer exports
```

## Recommended runtime layout

Use a state directory outside the repository:

```bash
STATE_DIR=/opt/data/hermes-social
META_STATE_DIR=$STATE_DIR/meta-comment-dm-automation
TIKTOK_STATE_DIR=$STATE_DIR/tiktok-backoffice
CAMOFOX_STATE_DIR=$STATE_DIR/camofox-data
```

The exact paths can differ by deployment. What matters is that runtime data is mounted into containers and ignored by git.

## Environment templates

Publish only templates:

```text
.env.example
deploy/.env.example
```

Template values should be placeholders, not real credentials. Prefer comments for secret fields so automated scanners do not confuse examples with leaked values:

```bash
META_APP_ID=your_meta_app_id
# META_APP_SECRET is required; set it privately in your deployment environment.
TIKTOK_BACKOFFICE_DB=/data/tiktok-backoffice.sqlite3
```

Never include real token values in examples, logs, screenshots, tests, or documentation.

## Before every commit

Run basic checks:

```bash
git status --short
git diff --check
git diff --cached --check
git diff --cached | grep -Ei 'access_token|refresh_token|client_secret|app_secret|password|bearer|sessionid|sid_guard|msToken|sk-' && exit 1 || true
```

Also inspect newly added files manually:

```bash
git diff --cached --name-only
```

## Hermes local workspace

`.hermes/` is treated as a local agent workspace and ignored by git. If a Hermes plan or note becomes useful to contributors, copy it into `docs/` and sanitize it first.

Sanitization checklist:

- Replace absolute personal/VPS paths with documented variables.
- Remove credentials, tokens, cookies, screenshots, and database paths that reveal private state.
- Convert task-progress notes into durable documentation.
- Keep operational claims reproducible with commands and tests.
