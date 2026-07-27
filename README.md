# Hermes Social

[Hermes Agent](https://hermes-agent.nousresearch.com) social media automation suite.
Automate DM replies, public replies, and comment processing across **Meta (Facebook/Instagram)**, **TikTok**, and **YouTube** — all from one Hermes Agent instance.

## Architecture

```
hermes-social/
├── core/              # Vendored Hermes Agent (MIT, Nous Research)
│                      # Never modify — replaced wholesale on updates
├── social/
│   ├── tools/
│   │   ├── meta-webhook/       # Facebook & Instagram comment automation
│   │   ├── tiktok-backoffice/  # TikTok DM & review pipeline
│   │   └── youtube-backoffice/ # YouTube reply automation
│   ├── skills/       # Social media skills loaded by Hermes
│   └── config/       # Config snippets
├── deploy/            # Compose stack, Dockerfile, env template
├── docs/              # Architecture, security, roadmap
└── VERSION            # Current release version
```

## Quick start

```bash
git clone https://github.com/Dupflo/hermes-social
cd hermes-social/deploy
cp .env.example .env
vim .env              # fill in secrets
docker compose up -d  # starts hermes + camofox + meta-webhook
```

Then visit the Hermes UI via SSH tunnel:

```bash
ssh -L 4860:127.0.0.1:4860 root@<vps>
# Open http://localhost:4860
```

## Documentation

Consultez le [guide utilisateur](docs/guide/index.md) pour la configuration complète :

| Guide | Description |
|-------|-------------|
| [Architecture](docs/guide/architecture.md) | Vue d'ensemble du système |
| [Meta (Facebook/Instagram)](docs/guide/setup-meta.md) | Création app Facebook, webhook, tokens |
| [Domaine / DNS](docs/guide/setup-domain.md) | OVH, DNS, HTTPS avec Caddy/Traefik |
| [TikTok + Camofox](docs/guide/setup-tiktok.md) | Session navigateur, DM automatisé |
| [YouTube OAuth](docs/guide/setup-youtube.md) | API, réponses publiques |

## Platforms

```bash
git clone https://github.com/Dupflo/hermes-social
cd hermes-social/deploy
cp .env.example .env
vim .env                    # fill in secrets
docker compose up -d        # starts hermes + camofox + meta-webhook
```

Then visit the Hermes UI via SSH tunnel:

```bash
ssh -L 4860:127.0.0.1:4860 root@<vps>
# Open http://localhost:4860
```

## Platforms

### Meta (Facebook / Instagram)
- Webhook-based: receives comment events in real time
- Public replies + private DMs via Graph API
- Keyword matching (proxy, système, markitdown, etc.)
- Interest-only keywords (migration → manual queue)
- Supports Instagram private replies

### TikTok
- Browser-based via Camofox (playwright + X11)
- Video DOM verification before any action
- Scoped DM history audit to prevent duplicates
- Automatic DM send with X11 clipboard paste
- Public fallback detection surveillance
- Cron: every 15 min

### YouTube
- YouTube Data API v3 (read) + OAuth 2.0 (write)
- Keyword matching on recent video comments
- Public replies with resource link
- Cron: every 15 min

## Configuration

### Secrets (never committed)

| File | Purpose |
|------|---------|
| `deploy/.env` | Main secrets (tokens, keys) |
| `/opt/data/youtube-backoffice.env` | YouTube API key + refresh token |
| `/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3` | Runtime TikTok DB |
| `social/tools/meta-webhook/.env` | Meta Graph API tokens |
| `social/tools/youtube-backoffice/client_secret.json` | OAuth client secret |

### Environment Variables

Each tool has its own `.env.example`:

| Tool | Path |
|------|------|
| Meta webhook | `social/tools/meta-webhook/.env.example` |
| TikTok | `social/tools/tiktok-backoffice/.env.example` |
| YouTube | `social/tools/youtube-backoffice/.env.example` |

## Crons (Hermes-managed)

All run as `no_agent=true` scripts (no LLM token cost):

| Cron | Interval | What it does |
|------|----------|-------------|
| TikTok auto-DM | 15 min | Scan, ingest, DM verified users |
| TikTok fallback surveillance | 30 min | Detect replies/DM on fallback items |
| YouTube auto-reply | 15 min | Scan comments, post public reply with link |
| Update checker | Daily | Alert when new version available |

Crons are silent when nothing needs attention — they only notify on Telegram on changes.

## Updating

```bash
cd /opt/repos/hermes-social
git pull
git checkout tags/$(curl -sL https://api.github.com/repos/Dupflo/hermes-social/releases/latest | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
cd deploy
docker compose build --no-cache
docker compose up -d
```

Or use the built-in update checker (cron notifies on new versions).

## Security

- **Zero secrets in repo.** All tokens, keys, and DBs are gitignored.
- All ports bind to `127.0.0.1` by default — SSH tunnel only.
- Camofox persistence uses IndexedDB for TikTok session stability.
- GitHub auth uses VPS-hosted credentials, not repo-stored secrets.

## License

MIT. `core/` is © Nous Research; the social layer is © Florian Dupuis.
