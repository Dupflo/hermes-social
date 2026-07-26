# social/tools

Standalone services and scripts that support the social skills — things that
run *next to* the agent rather than being invoked as a skill.

Planned:

- `meta-webhook/` — Meta comment/DM webhook receiver (currently running ad hoc
  on the main VPS as `meta-webhook-meta-webhook-1`; to be migrated here,
  sanitized of any credentials first).

Rules:

- No secrets in this tree, ever — the repo is public. Config comes from
  environment variables declared in `deploy/.env.example`.
- Each tool gets its own directory with a README and, if containerized, its
  own Dockerfile or compose service entry.
