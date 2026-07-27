# Social Tools

Standalone services for social media automation, managed by Hermes Agent.

| Tool | Directory | Platform | Approach |
|------|-----------|----------|----------|
| Meta Webhook | `meta-webhook/` | Facebook, Instagram | Graph API webhooks + OAuth |
| TikTok Backoffice | `tiktok-backoffice/` | TikTok | Camofox browser automation |
| YouTube Backoffice | `youtube-backoffice/` | YouTube | Data API + OAuth 2.0 |

## Requirements

- Docker (for Camofox browser container)
- Hermes Agent with cron support
- Platform-specific API tokens (see each tool's README)

## Architecture

Each tool is independent — they share no runtime state. The Hermes Agent
orchestrates them through crons (for polling) and webhooks (for events).

## Security

- **No secrets in source.** Each tool reads `.env` from its own directory
  (gitignored). YouTube uses OAuth `client_secret.json` (gitignored).
- **No credentials in logs.** All token values are masked in output.
- **No public interfaces.** Containers bind to `127.0.0.1`; reach them via
  SSH tunnel or Hermes proxy.

## Quick start

```bash
# Each tool has its own dependencies
cd meta-webhook && uv sync
cd ../tiktok-backoffice && uv sync
cd ../youtube-backoffice && # uses system Python, no uv needed
```
