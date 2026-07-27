# TikTok Backoffice

Browser-based TikTok comment automation using Camofox.

## How it works

1. **Discovery** — scans configured TikTok profiles for new comments matching keywords
2. **Ingestion** — fetches comment threads via Camofox, deduplicates, stores in SQLite
3. **Review pipeline** — each comment is classified (pending / ignored / needs manual)
4. **DM automation** — opens TikTok profile, checks DM history (scoped anti-dedup), sends DM via X11 clipboard paste, verifies delivery
5. **Surveillance** — monitors fallback comments for public replies or incoming DMs

## Files

| Path | Purpose |
|------|---------|
| `app/camofox_reader.py` | Camofox DOM extraction logic |
| `app/store.py` | SQLite models and DB operations |
| `app/cli.py` | Review queue CLI (`uv run tiktok-backoffice`) |
| `scripts/tiktok_cron_auto_dm.py` | Main cron worker (scan + DM) |
| `tests/` | Pytest test suite |

## Requirements

- Camofox container running (`hermes-social-camofox-1`)
- `xclip` and `xdotool` installed in the Camofox container
- A Kanban database (`/opt/data/kanban/boards/meta-campaigns/kanban.db`)
- Campaign source tasks with DM text stored in Kanban

## Cron jobs

| Name | Interval | Script |
|------|----------|--------|
| TikTok auto-DM | 15 min | `tiktok_cron_auto_dm.py --apply` |
| Fallback surveillance | 30 min | `tiktok_fallback_surveillance.py` |

Both run as `no_agent=true` scripts — no LLM token cost.

## Security

- Runtime DB at `/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3` (gitignored)
- No tokens or secrets — TikTok auth is handled via Camofox persistent storage (IndexedDB)
- All screenshots and logs are gitignored
