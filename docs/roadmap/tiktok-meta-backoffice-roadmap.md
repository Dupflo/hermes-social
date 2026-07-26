# TikTok + Meta Social Backoffice Roadmap

This roadmap describes the open-source architecture for a Hermes-driven social backoffice that handles Meta first, then TikTok through a safe review workflow.

## Goal

Stabilize Meta as the production API-first channel, then add TikTok with a realistic architecture:

```text
read/poll -> local database -> Hermes/Telegram review -> browser-assisted action -> explicit human approval
```

The project intentionally avoids claiming Meta-like TikTok capabilities where they do not exist for standard social-video comments and DMs.

## Architecture

### Meta

Meta is API-first when the app has the required permissions:

- webhooks for comments/messages,
- Graph API comment replies,
- private replies / DMs where permitted,
- persistent state in a server-side state directory.

### TikTok social comments

TikTok is split into two layers:

1. **Read layer**
   - Preferred official path if available: TikTok Research API / approved read access.
   - Practical fallback: read-only browser/Camofox scanning.
2. **Action layer**
   - Store matched comments locally.
   - Show one review item at a time in Hermes/Telegram.
   - Use browser-assisted actions only after explicit approval and safety checks.

TikTok standard social-video comments are **not** treated as a Meta clone. The tool should not expose private resource links publicly.

### TikTok Shop

TikTok Shop is a separate track. Shop webhooks/customer-service APIs may be useful for shop conversations, products, orders, and affiliate workflows, but they are not proof of support for standard video comment-to-DM automation.

Only build the Shop track when the operator confirms Seller/Partner access and a real Shop use case.

## Recommended repository layout

```text
hermes-social/
  README.md
  .env.example
  docs/
    architecture/
    roadmap/
    runbooks/
    security/
  deploy/
    docker-compose.yml
    .env.example
  social/
    tools/
      meta-webhook/
      tiktok-backoffice/
  scripts/
```

## Runtime directories

Do not hardcode one person's VPS paths in application logic. Document them as environment variables instead:

```bash
REPO_DIR=/opt/repos/hermes-social
STATE_DIR=/opt/data/hermes-social
META_STATE_DIR=$STATE_DIR/meta-comment-dm-automation
TIKTOK_STATE_DIR=$STATE_DIR/tiktok-backoffice
TIKTOK_DB=$TIKTOK_STATE_DIR/tiktok_backoffice.sqlite3
```

Local deployments can choose different paths as long as secrets and runtime state remain outside git.

## Execution phases

### Phase 0: Protect current state

- Inspect git status before operational changes.
- Keep code commits separate from runtime state.
- Never copy `.env`, databases, logs, tokens, cookies, browser profiles, or screenshots into git.

### Phase 1: Stabilize Meta

- Keep Meta webhook code in the public repo.
- Keep Meta state under a server-side state directory.
- Provide `.env.example` for required variables.
- Run tests before deployment.

Example:

```bash
cd "$REPO_DIR"
uv run pytest social/tools/meta-webhook/tests -q
```

### Phase 2: Document reusable operating procedures

Move reusable knowledge into versioned docs/runbooks, not local agent scratch files.

Good candidates:

- deployment runbooks,
- Camofox/noVNC operations,
- token-renewal procedures without token values,
- troubleshooting checklists,
- architecture diagrams.

### Phase 3: TikTok non-Shop production path

TikTok should remain human-gated until the flow is stable.

Core pipeline:

```text
video registry
  -> comment reader
  -> campaign matcher
  -> review item
  -> approved browser action
  -> evidence/status record
```

Important safety rules:

- Target comments by stable author/text/context, not visual position only.
- Verify the page is connected and belongs to the target video.
- Verify the creator has not already replied.
- Like only the intended comment.
- Send private resource links only through verified private delivery.
- If DM fails or privacy blocks delivery, use a public fallback asking the commenter to message first.
- Never post public resource links.

### Phase 4: End-to-end dry runs

Use dry-run/read-only paths first:

```bash
cd social/tools/tiktok-backoffice
uv run pytest -q
uv run tiktok-backoffice fetch-comments-camofox --video-url '<video-url>'
uv run tiktok-backoffice next-review
```

Browser publishing should remain disabled until explicit operator approval is implemented and tested.

## Risks

- TikTok DOM and anti-bot behavior can change.
- Browser sessions may show login gates despite persisted cookies.
- TikTok can show the target URL/title while the DOM belongs to a recommended video.
- DM deliverability cannot be assumed from a visible composer alone.
- Fully automatic posting may create duplicate or unsafe public replies.

## Open questions

- Which TikTok read path is acceptable for the deployment: Research API, browser read-only, or both?
- Should TikTok publishing remain manual forever, or only permitted after explicit Telegram confirmation?
- Is TikTok Shop in scope for this deployment?
