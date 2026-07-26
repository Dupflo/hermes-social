# social/tools

Standalone services and scripts that support the social skills — things that
run *next to* the agent rather than being invoked as a skill.

## meta-webhook/

Meta comment→DM automation (FastAPI): a user comments a keyword (e.g. `proxy`)
under an Instagram/Facebook post → the app likes the comment, replies publicly,
and DMs the resource link. Includes token renewal, campaign rules, backfill
tooling and tests.

- Runs as the `meta-webhook` compose service (see `deploy/docker-compose.yml`),
  bound to `127.0.0.1:8791` — expose it to Meta through your reverse proxy.
- Config via `META_*` variables in `deploy/.env` (template:
  `meta-webhook/.env.example`).
- History note: originally developed in place on the main VPS by the Hermes
  agent (repo `Dupflo/meta-comment-dm-automation`). This tree is now the home;
  the old repo is an archive. Runtime state (`data/`, sqlite, logs) stays out
  of git.

Rules:

- No secrets in this tree, ever — the repo is public. Config comes from
  environment variables declared in `deploy/.env.example`.
- Each tool gets its own directory with a README and, if containerized, its
  own Dockerfile or compose service entry.
## tiktok-backoffice/

Draft-only TikTok comment backoffice helpers. The current implementation stores
comments and suggested replies locally, but never publishes to TikTok. Runtime
state and TikTok session material stay under `/opt/data`, outside the public
repo.
