# Meta Webhook

Facebook and Instagram comment automation via Meta Graph API.

## How it works

1. **Webhook** — receives real-time comment events from Meta (Facebook + Instagram)
2. **Keyword matching** — matches comments against configured campaign keywords
3. **Public reply** — posts a reply on the comment via Graph API
4. **Private DM** — sends a private reply with the resource link (Facebook and Instagram)

## Architecture

```
┌─────────────┐     Webhook POST     ┌──────────────────┐
│   Meta      │ ──────────────────── │  FastAPI server   │
│ (Graph API) │                      │  (port 8791)      │
└─────────────┘                      └────────┬─────────┘
                                              │
                                    ┌─────────▼─────────┐
                                    │   SQLite (state)   │
                                    │ processed_comments │
                                    │ campaign_rules     │
                                    │ comment_reviews    │
                                    └───────────────────┘
```

## Configuration

Copy `.env.example` to `.env` and fill in:

| Variable | Source |
|----------|--------|
| `META_APP_ID` | Meta Developer App |
| `META_APP_SECRET` | Meta Developer App |
| `META_PAGE_ACCESS_TOKEN` | Page → Messenger → API |
| `META_PAGE_ID` | Your Facebook Page ID |
| `META_IG_USER_ID` | Instagram Business Account ID |
| `META_VERIFY_TOKEN` | Your chosen webhook verify token |
| `META_OWNER_USERNAMES` | Your social handles (for dedup) |
| `RESOURCE_KEYWORD` | Default keyword to match |
| `RESOURCE_URL` | Default resource link to DM |
| `INTEREST_ONLY_KEYWORDS` | Keywords that are interest signals only |

## API Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/health` | GET | Health check |
| `/webhook/meta` | GET | Meta webhook verification |
| `/webhook/meta` | POST | Receive comment events |
| `/privacy` | GET | Privacy policy page |
| `/data-deletion` | GET | Data deletion instructions |

## Background scanning

In addition to webhook events, the service can scan past comments:

```bash
uv run python app/scan_comment_reviews.py --dry-run
```

## Webhook subscription troubleshooting

If webhooks return 403:
1. Check `META_VERIFY_TOKEN` matches what's configured in Meta Developer Dashboard
2. Check `META_APP_SECRET` matches
3. Verify the webhook URL is reachable from Meta servers

## Security

- **No secrets in source.** `.env` is gitignored.
- All endpoints except `/webhook/meta` are unauthenticated (public routes).
- Webhook signature verified via `X-Hub-Signature-256`.
- Port 8791 binds to `127.0.0.1` — front with Caddy/Traefik for public access.
