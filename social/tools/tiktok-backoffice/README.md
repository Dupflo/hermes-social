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
| `app/tiktok_login.py` | TikTok login for the hermes-console job `tiktok-login` (`python -m app.tiktok_login`, s21) |
| `app/tiktok_login_selectors.py` | The only module that knows the TikTok login DOM (URL, selectors, passive probe, masking) |
| `scripts/console-tiktok-login.sh` | Wrapper of the login, run by the console worker (arguments forwarded) |
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

## Login (hermes-console job `tiktok-login`, s21)

`python -m app.tiktok_login` (wrapper `scripts/console-tiktok-login.sh`) reconnects the Camofox profile to
TikTok. The console starts it only after a `logged_out` verdict of the session check (or a manual run), at most
`TIKTOK_LOGIN_MAX_ATTEMPTS_24H` times per 24 h (console side, console ADR 050).

1. It reads `storage_state` first: session cookies present => `skipped`/`already_connected`, nothing typed.
2. Otherwise it opens its own tab, navigates **once** to the e-mail login page, types the identifier and the
   password with real keystrokes (`POST /tabs/:id/type`, `mode: keyboard`), submits, and polls a passive probe
   (the page is never loaded again, captcha included).
3. At the verification code screen it waits for the code sent from the console (see below), keeping the tab
   alive with a passive `evaluate` at least every 60 s.

Outcomes (last line `CONSOLE_RESULT {json}`, console ADR 011):

- `ok` with reason `connected` (the `storage_state` read that sees the cookies also saves the profile),
  `captcha_required`, `verification_code_required` (code screen without a channel), `code_timeout`,
  `code_rejected` (3 refused codes), `bad_credentials` or `login_unknown`; counter `attempted = 1` as soon as
  the login page is navigated to;
- `skipped` with reason `already_connected`, `credentials_missing`, `camofox_down`, `camofox_auth_missing`
  or `camofox_auth_rejected` (`attempted = 0`);
- exit 14 (unexpected Camofox response or deadline), 15 (tabs saturated), 16 (internal error).

On every failure outcome the identifier, password and code fields are masked, then a screenshot is written to
`/opt/data/tiktok-backoffice/screenshots/login-<epoch_ms>.png` (`0600`, the 20 newest kept); its time is the
`screenshot_at` counter. A failed mask means no screenshot. The output never holds the identifier, the
password, the code nor an exception message.

`--check-only` reads the cookies and stops: `skipped` with `already_connected` or `would_attempt`; it never
navigates nor types (deployment check).

Verification code channel (files under `/opt/data/console-runs`, the runs directory shared with the console
worker, `0600`):

| File | Written by | Purpose |
|------|------------|---------|
| `tiktok-login-<CONSOLE_RUN_ID>.awaiting.json` | this script | `round`, `expires_at_ms`, `screenshot_at`, `rejected_before`: the console shows the form |
| `tiktok-login-<CONSOLE_RUN_ID>.code` | console worker | the code typed in the console; read then deleted before typing |

Both are deleted at the end of the run, whatever the outcome.

Environment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `TIKTOK_USERNAME` | — | TikTok login (vault value injected by the console, else `deploy/.env`) |
| `TIKTOK_PASSWORD` | — | TikTok password (same sources), never printed |
| `TIKTOK_LOGIN_CODE_WAIT_SEC` | `600` | Code wait window, bounded to [120, 600] |
| `CONSOLE_RUN_ID` | — | Console run id, injected by the console; without it the code screen is final |

Selectors could not be checked against the real TikTok page: an unrecognised page ends `login_unknown` with a
masked screenshot, and `app/tiktok_login_selectors.py` is the only file to fix.

## Security

- Runtime DB at `/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3` (gitignored)
- No tokens or secrets — TikTok auth is handled via Camofox persistent storage (IndexedDB)
- All screenshots and logs are gitignored
