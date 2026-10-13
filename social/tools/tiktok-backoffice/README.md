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
| `app/session_check.py` | TikTok session check for the hermes-console job `tiktok-session-check` (`python -m app.session_check`) |
| `scripts/console-session-check.sh` | Wrapper run by the console worker (exec in `hermes-social-hermes-1`, console venv) |
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

## Session check (hermes-console job `tiktok-session-check`, s20)

`python -m app.session_check` (wrapper `scripts/console-session-check.sh`) tells the console whether the
Camofox profile is logged in to TikTok. It prints one state line, then `CONSOLE_RESULT {json}` as the last
line (console ADR 011):

- `ok` with reason `connected`, `logged_out` (cookies `sessionid` + `sid_tt` on `tiktok.com`, read through
  `GET /sessions/<userId>/storage_state`), `captcha` (passive selector check on the TikTok tabs already open)
  or `camofox_down`;
- `skipped` with reason `camofox_auth_missing` (no key) or `camofox_auth_rejected` (403);
- exit 11 (unexpected Camofox response or deadline), 12 (tabs saturated), 13 (internal error), without
  `CONSOLE_RESULT`.

It never navigates nor reloads a tab, opens an `about:blank` tab only when the session has none (closed right
after), and never prints a cookie value, the key or an exception message. Every read of `storage_state` also
saves the profile to disk (persistence plugin).

Environment (also read by `fetch-comments-camofox` when `--base-url`/`--user-id` are omitted):

| Variable | Default | Purpose |
|----------|---------|---------|
| `CAMOFOX_URL` | `http://camofox:9377` | Camofox API base URL |
| `CAMOFOX_USER_ID` | `hermes_80317d7dba` | Camofox profile shared with the Hermes agent |
| `CAMOFOX_API_KEY` | empty | Bearer for the protected Camofox routes; set in `deploy/.env`, same value for the `camofox` and `hermes` services (console ADR 049) |

## Security

- Runtime DB at `/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3` (gitignored)
- No tokens or secrets — TikTok auth is handled via Camofox persistent storage (IndexedDB)
- All screenshots and logs are gitignored
