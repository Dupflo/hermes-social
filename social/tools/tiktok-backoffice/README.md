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
| `app/captcha_solver.py` | TikTok captcha solver, `solve_captcha` (s22, reusable by the scan) |
| `app/captcha_selectors.py` | The only module that knows the captcha DOM (8 variants, passive page scripts) |
| `app/captcha_geometry.py` | Pure identification and SadCaptcha answer → mouse gesture conversion |
| `app/sadcaptcha.py` | SadCaptcha HTTP client (stdlib, key never printed) |
| `app/tiktok_scan.py` | Comment scan of the hermes-console job `tiktok-scan` (s23; no queue since s24) |
| `app/tiktok_reply.py` | Replies and DMs of the hermes-console job `tiktok-reply` (`python -m app.tiktok_reply`, s24) |
| `app/tiktok_reply_page.py` | Browser gestures and page verifications of `tiktok-reply` (keystrokes, clicks, no clipboard) |
| `app/tiktok_reply_selectors.py` | The only module that knows the comments panel, profile and DM thread DOM (read-only scripts) |
| `scripts/console-tiktok-reply.sh` | Wrapper of `tiktok-reply`, run by the console worker (arguments forwarded) |
| `scripts/tiktok_cron_auto_dm.py` | Main cron worker (scan + DM), legacy, kept until the s24 cutover |
| `tests/` | Pytest test suite |

## Requirements

- Camofox container running (`hermes-social-camofox-1`)
- `xclip` and `xdotool` installed in the Camofox container (legacy processor only; `tiktok-reply` needs neither)
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

## Captchas (s22, hermes-console ADR 052/053)

When a probe of the login sees a captcha (before the form, after the submit, after the code), the login hands it
to `app.captcha_solver.solve_captcha`:

- **Key**: `SADCAPTCHA_API_KEY`, vault value injected by the console into the exec `Env`, else `deploy/.env`.
  Absent from both ⇒ solver disabled: the captcha is logged `disabled` and the login concludes
  `captcha_required` (manual login in the console Navigator), without error. To switch the solver off, remove the
  key from both. The key travels in the SadCaptcha URL: it is never printed, nor any URL or exception message.
- **Gestures**: trusted Playwright mouse events through the Camofox plugin `console-mouse`
  (`deploy/camofox-plugins/console-mouse`, mounted as its own sub-directory
  `./camofox-plugins/console-mouse:/app/plugins/console-mouse:ro` — never on `/app/plugins`, which would hide the
  image's persistence/vnc/youtube plugins — and declared in `deploy/camofox.config.json`). It is only loaded after
  camofox is recreated. Before any paid call, the solver probes `GET /console-mouse/health`
  (`CamofoxClient.mouse_available`): 404 (plugin not loaded), 403 or unreachable ⇒ `unavailable`, manual login.
- **Bounds**: at most 3 tries per captcha and 3 captchas per run; a budget of `SOLVER_BUDGET_S` = 240 s per run,
  suspended from the 180 s login deadline; a try only starts with ≥ 60 s left. A SadCaptcha error stops solving
  for the whole run (no retry). The page is never reloaded; rotate v2 uses the captcha's own refresh button.
- **Outcomes** of a captcha: `solved` | `unsolved` (3 failed tries, or budget out after ≥ 1 try) |
  `unrecognized` (no known variant: never a random click) | `disabled` | `unavailable` | `service_error`.
  Result of a try: `solved` | `failed` | `unrecognized`.
- **Report**: every captcha met goes into `CONSOLE_RESULT` under the key `captchas`
  (`[{seq, outcome, at, attempts: [{try, type, variant, result, ms}]}]`, ≤ 5 captchas, ≤ 3 tries — format of the
  console ADR 052), plus the counters `captchas`, `captchas_solved` and `captcha_tries`. On exit 14/15/16 the line
  is still emitted, last, when captchas were met; its `status` is meaningless (the exit code wins). The console
  worker stores it in `console.sqlite3` (7-day success rate, alert under 80 %).
- **Reuse (s23)**: `solve_captcha(client, tab_id, *, api_key, report, clock, sleep, rng, deadline)` knows nothing
  of the login; the scan can call it as is.

The captcha selectors (official client of 2026-09-25) could not be checked against the real TikTok:
`app/captcha_selectors.py` is the only file to fix.

## Scan console (s23)

The hermes-console job `tiktok-scan` (every 15 min, console ADR 054, amended by console ADR 055 in s24) runs
`scripts/console-tiktok-scan.sh` → `python -m app.tiktok_scan` in the console venv, inside the `tiktok-browser` lock
group. It replaces the scan loop of the Hermes cron `bf85146a1ae8` (`scripts/tiktok_cron_auto_dm.py --apply`), minus
`sync-kanban-campaigns` (job `campaign-sync`) and, since s24, minus the queue (job `tiktok-reply`, below).

- **Session guard (console side)**: the job never starts unless the last `tiktok-session-check` verdict is
  `connected` and that check is enabled ⇒ otherwise `skipped`/`no_session`, no exec.
- **Legacy cron guards** (never both at once): a process naming `tiktok_cron_auto_dm` in `/proc` at start ⇒
  `skipped`/`legacy_cron_running`; before each video (bound 700 s: worst case ~382 s + 240 s of solver + one
  try's overrun) the module reads `/opt/data/cron/jobs.json` **read only** (`enabled`, `state`, `next_run_at` of
  `bf85146a1ae8`, nothing printed) and stops when the cron may start within the bound ⇒ `skipped`/`legacy_cron_due`.
  Unreadable file ⇒ due.
- **Scan**: `poll_targets(8)`, least recently attempted first; every attempt (read, empty, failed) dates
  `last_scanned_at` (`store.mark_scan_attempt`), so failures no longer hog the head. Fetch in process, own tab
  (closed after), at most 9 Camofox tabs, 240 s per video; a captcha goes to the s22 solver in the current tab,
  never after a reload (solver time suspended from the video deadline, 240 s and 3 captchas per run; a further
  captcha is refused without a solver call and logged `unavailable`). No new video after 2160 s. Videos not
  attempted are not dated: first in line next run.
- **Baseline (s24)**: comments are ingested as a console read (`ingest_comments(console_read=True)`), which dates
  `tiktok_videos.baseline_at` in the ingestion's own transaction. The first successful console read of a video never
  read before (no baseline, no comment) is a baseline: its review items are created `expired`/`first_scan_baseline`
  (TikTok dates are lost at extraction, an old comment would look new). Counter `reviews_baseline`, `baseline=<0|1>`
  per video.
- **Outcomes**:

  | Case | Run console |
  | --- | --- |
  | session not `connected` / check disabled | `skipped`/`no_session` (no exec) |
  | legacy cron running at start | `skipped`/`legacy_cron_running` |
  | legacy cron due before a video | `skipped`/`legacy_cron_due` |
  | 0 target video | `skipped`/`no_targets` |
  | captcha unsolved | `failed`/`exit_18` |
  | session lost (`tiktok_page_not_actionable`) | `failed`/`exit_20` |
  | tabs saturated before a video | next rule |
  | no attempted video read | `failed`/`exit_17` |
  | unexpected exception | `failed`/`exit_19` (class name only) |
  | otherwise | `ok`, `empty` after 4 decisive runs at `comments_extracted` = 0 |

  Output: `video <id>: extracted= new= reviews= error= baseline=`, a summary line, then `CONSOLE_RESULT` last (flat
  counters + `videos`/`captchas`), on every exit code.
- **`--check-only`**: no browser; prints `targets`, `pending`, `legacy_running`, `legacy_due`. Never run by the
  console.
- The legacy cron `scripts/tiktok_cron_auto_dm.py` stays in the repo as the parity oracle until the s24 cutover.
- **Versioned processor**: `scripts/process_tiktok_review_one.py` is the production copy of
  `/opt/data/tmp/process_tiktok_review_one.py`, versioned as is (byte for byte, sha256
  `08e5f29d0c87bd154588df5e603d0a5132814d3a420f03e742e39d9aa2892f93`, pinned by `tests/test_processor_versioned.py`).
  Since s24 the scan no longer calls it; only `tests/test_process_review_one.py` loads it, every side effect faked
  (characterization). It sends real TikTok messages when run.

## Réponses console (s24)

The hermes-console job `tiktok-reply` (every 15 min, console ADR 055, **delivered disabled**, enabled at the s24
cutover) runs `scripts/console-tiktok-reply.sh` → `python -m app.tiktok_reply` in the console venv, inside the
`tiktok-browser` lock group, under `timeout 1500`. It replaces the legacy processor and the detection of the Hermes
cron « TikTok fallback surveillance » (`42cade327881`).

- **Guards**: session (console side, `no_session`); legacy cron `bf85146a1ae8` running (`/proc`) or enabled and not
  paused in `/opt/data/cron/jobs.json` (unreadable ⇒ enabled) ⇒ `skipped`, **nothing written**; asked at start and
  before every item.
- **Expiration**: queue items ingested more than 7 days ago ⇒ `expired`/`older_than_7d`; items not ingested by a
  console read after their video's baseline (legacy path) ⇒ `expired`/`legacy_backlog`. No page, no budget.
- **Per item**: conditional claim `pending_review` → `sending` (never twice); campaign unknown / inactive / without
  `dm_text` ⇒ `failed` (`campaign_unknown`, `campaign_inactive`, `no_dm_text`) without browser; owner reply already
  under the comment and no own send row ⇒ `posted`/`already_present`, nothing sent; DM (text of
  `tiktok_campaigns.dm_text`, real keystrokes, `Shift+Enter` between lines) then the public reply
  (`tiktok_review_items.reply_text`), each **verified in the page**; every clicked send writes a
  `tiktok_outbound_messages` row (`sent` | `failed`) at once. No « J'aime ».
- **Hourly limit**: `TIKTOK_REPLY_MAX_PER_HOUR` (1-60, default 10, from the console registry `execEnv`), counted in
  `tiktok_outbound_messages` over a sliding hour. Budget too low at start ⇒ `skipped`/`rate_limited`; spent during
  the run ⇒ `ok`/`rate_limited`, the rest stays queued.
- **Owner**: `TIKTOK_OWNER_HANDLE` (`@handle`, from the registry `execEnv`), never hard-coded.
- **Outcomes**:

  | Case | Run console |
  | --- | --- |
  | invalid owner, limit or `--max-items` | `failed`/`exit_25` (no store access) |
  | legacy cron running / enabled | `skipped`/`legacy_cron_running` / `legacy_cron_enabled` |
  | empty queue after expiration | `ok`/`no_pending` |
  | budget too low at start | `skipped`/`rate_limited` |
  | budget spent during the run | `ok`/`rate_limited` |
  | Camofox unreachable, tabs full, page unusable before any action | item back to the queue, `failed`/`exit_21` |
  | captcha unsolved before any action | item back to the queue, `failed`/`exit_22` |
  | unexpected exception / SIGTERM | `failed`/`exit_23` (class name only; an item with a send left `sending`) |
  | session lost before any action | item back to the queue, `failed`/`exit_24` |
  | otherwise | `ok` |

- **`--check-only`**: no browser, no write besides the schema; prints `pending`, `expirable`, `legacy_backlog`,
  `legacy=<none|enabled|running>`, `budget`, `owner=<ok|missing>`, `captcha_key=<ok|missing>` (never the key) and
  `dm_text_campaigns=<with>/<active>`.
- **`--max-items N`** (1-5): CLI only (first observed send of the cutover), never passed by the console.
- The legacy processor and `scripts/tiktok_cron_auto_dm.py` stay in the repo until the s24 cutover.

## Security

- Runtime DB at `/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3` (gitignored)
- No tokens or secrets — TikTok auth is handled via Camofox persistent storage (IndexedDB)
- All screenshots and logs are gitignored
