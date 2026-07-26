# TikTok Backoffice

Draft-only TikTok comment backoffice helpers for Hermes Social.

## Safety contract

This tool **does not publish TikTok replies**. It only stores comments, matches
local keyword campaigns, and prepares suggested reply drafts for Hermes/operator
review.

- Runtime state lives outside git, e.g. `/opt/data/tiktok-backoffice/`.
- TikTok login/session material stays outside git, e.g.
  `/opt/data/meta-comment-dm-automation/data/tiktok_login_tool_token.txt`.
- Repo is public: never commit cookies, tokens, screenshots with private data, or
  client/customer data.

## Commands

```bash
uv run tiktok-backoffice add-comment   --video-url 'https://www.tiktok.com/@dupflodev/video/123'   --comment-id 'comment-1'   --author '@someone'   --text 'proxy'

uv run tiktok-backoffice draft --comment-id comment-1 --keyword proxy   --reply 'Je te mets le lien ici 👍'

uv run tiktok-backoffice captcha-needed \
  --video-url 'https://www.tiktok.com/@dupflodev' \
  --screenshot-path /opt/data/browser_screenshots/captcha.png

uv run tiktok-backoffice browser-draft-filled \
  --video-url 'https://www.tiktok.com/@dupflodev/video/7665295144496762134' \
  --screenshot-path /opt/data/browser_screenshots/draft.png

uv run tiktok-backoffice browser-events
uv run tiktok-backoffice next
uv run tiktok-backoffice list --status drafted_in_browser

# Create/update a local keyword campaign
uv run tiktok-backoffice campaign-upsert \
  --slug proxy \
  --name 'Proxy' \
  --keywords 'proxy,proxies' \
  --reply 'Je te mets le lien ici 👍'

# Match pending comments against a campaign and create review items idempotently
uv run tiktok-backoffice match --campaign proxy

# Fetch the next campaign/comment item for Hermes/Kanban review
uv run tiktok-backoffice next-review

# Approve a review item for browser draft preparation (still no publish)
uv run tiktok-backoffice approve-draft --review-id 1

# Ignore a review item with an operator reason
uv run tiktok-backoffice ignore-review --review-id 1 --reason "hors sujet"

# Inspect one review item
uv run tiktok-backoffice review --review-id 1

# Browser worker queue: oldest review approved for draft
uv run tiktok-backoffice next-browser-draft

# Browser worker result: composer filled, screenshot captured, still not posted
uv run tiktok-backoffice browser-drafted --review-id 1 --screenshot-path /opt/data/browser_screenshots/draft.png
```

`draft` records a draft locally and returns the exact text to review. It does
not click, type, or post in TikTok.

## Future browser-assisted step

When Camofox is available, the next incremental command should be a separate
`browser-draft` action that opens TikTok with Hermes/Camofox, finds the comment,
puts the reply text in the reply box, captures evidence, and stops before the
Publish/Post action.

## Live TikTok browser reality

A first live Hermes browser check against `https://www.tiktok.com/@dupflodev`
showed TikTok's slider CAPTCHA. The correct behavior is to record
`needs_manual_captcha` and ask the operator to solve it through Camofox/noVNC,
not to automate drag gestures blindly or claim discovery succeeded.

## Campaign review flow

TikTok campaigns are local SQL objects: a slug, display name, comma-separated
keywords, and a proposed reply template. `match --campaign <slug>` scans
`pending_review` / `needs_review` comments, creates one review item per
`comment_id + campaign_slug`, and is safe to run repeatedly.

`next-review` returns the oldest pending review item with the source comment,
matched keyword, campaign slug, and proposed reply text. Hermes/Kanban should
use that item as the human approval unit before any browser draft or publish
action.

TikTok Open API does not currently provide a public Meta-like comment webhook,
public comment reply endpoint, or DM/private-reply endpoint for standard social
videos. This tool is therefore read/poll + review + browser-assisted action.

Review decisions are separate from publishing:

- `approve-draft` moves a review item to `approved_for_draft`; a future browser
  worker may fill the TikTok composer from this state, but must still stop before
  `Post`.
- `ignore-review` records a human/operator reason and removes the item from the
  pending queue.
- Publishing will require a later explicit `approved_for_publish` state and a
  separate guarded command; it is intentionally not implemented here.

Browser worker integration contract:

- `next-browser-draft` returns only review items already approved by the
  operator with `approve-draft`.
- A browser worker may use the returned `video_url`, `author`, `comment_text`,
  and `reply_text` to fill TikTok's reply composer.
- After filling the composer, the worker must call `browser-drafted` with a
  screenshot path. This records `drafted_in_browser` and a browser event.
- `browser-drafted` still means **not posted**. There is intentionally no publish
  command in this increment.

## Reply template rule

Campaign reply text must be the exact operator-configured message for the
resource/campaign. Do **not** use a default template asking the commenter to DM
first. If a platform blocks the intended private/public reply flow, surface the
item to Hermes/Kanban for explicit operator approval instead of inventing a
DM-first fallback.
