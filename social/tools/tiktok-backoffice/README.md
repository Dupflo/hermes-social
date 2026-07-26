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

uv run tiktok-backoffice draft --comment-id comment-1 --keyword proxy   --reply 'Je viens de t’envoyer le lien en message privé'

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
  --reply 'Je viens de t’envoyer le lien en message privé'

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



## Profile video discovery

`discover-videos` registers recent videos from a TikTok profile. Direct HTTP
fetch can return zero on VPS/headless IPs because TikTok renders/blocks profile
grids dynamically; in that case, use the Hermes/Camofox browser to open the
profile and pass a saved HTML/snapshot file to the same parser.

```bash
# Best-effort direct profile fetch
uv run tiktok-backoffice discover-videos --profile @dupflodev

# Deterministic fallback from a saved profile HTML/snapshot
uv run tiktok-backoffice discover-videos \
  --profile @dupflodev \
  --html-file /path/to/profile.html
```

Discovery is idempotent: existing videos are not duplicated. It extracts the
TikTok `video_id` from URLs and stores captions when available from image alt or
accessibility labels. After discovery, run `suggest-video-campaigns` to propose
caption-keyword mappings.


## Import Meta Kanban campaigns into TikTok

TikTok campaign sync imports active resource-delivery campaign cards from the
Meta campaign Kanban into local TikTok campaigns. It copies only safe campaign
metadata: name, keywords, active flag, and the TikTok public confirmation text.
It does **not** copy private resource links into public replies.

```bash
# Import active Meta/Kanban resource campaigns into the TikTok local DB
uv run tiktok-backoffice sync-kanban-campaigns

# Inspect imported TikTok campaigns
uv run tiktok-backoffice list-campaigns

# Then propose video/campaign mappings from CTA captions like “Commente proxy”
uv run tiktok-backoffice suggest-video-campaigns
```

The default TikTok public reply template for imported campaigns is:

```text
Je viens de t’envoyer le lien en message privé
```

`sync-kanban-campaigns` ignores template cards, inactive cards, archived tasks,
cards without keywords, and cards without a private resource/DM configuration.
`send/comment` delivery remains separate and gated; this import only prepares
review/mapping data.

## Video/campaign registry

Use the video registry to avoid asking for TikTok IDs every time. Videos can be
added manually first, then later discovered automatically from the profile.

```bash
# Register a TikTok video to monitor
uv run tiktok-backoffice add-video \
  --video-url 'https://www.tiktok.com/@dupflodev/video/123' \
  --caption 'Proxy propre pour VPS'

# Assign the video to a local campaign
uv run tiktok-backoffice assign-video \
  --video-url 'https://www.tiktok.com/@dupflodev/video/123' \
  --campaign proxy \
  --source manual

# Inspect videos and campaign links
uv run tiktok-backoffice list-videos --with-campaigns

# Suggest campaign links from video captions and active campaign keywords
uv run tiktok-backoffice suggest-video-campaigns

# Approve a suggested video/campaign link for polling
uv run tiktok-backoffice approve-video-campaign \
  --video-url 'https://www.tiktok.com/@dupflodev/video/123' \
  --campaign proxy
```

`assign-video` is idempotent. Future automatic discovery can fill this same
registry and propose campaign links from captions/Meta reflections before
operator approval.

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



## Camofox comment fetcher

`fetch-comments-camofox` uses the existing Hermes/Camofox sidecar to open a TikTok
video with the persisted logged-in browser session. It is read-only and never
posts. The command is deliberately conservative: if TikTok only renders the video
shell, recommendations, or the "We're having trouble playing this video" state,
it returns `fetched: 0` instead of ingesting false positives.

```bash
# Read-only diagnostic: no database writes
uv run tiktok-backoffice fetch-comments-camofox   --video-url 'https://www.tiktok.com/@dupflodev/video/7665295144496762134'

# Optional safe ingestion: stores only comments extracted from explicit comment
# containers and creates review items for approved video/campaign mappings.
uv run tiktok-backoffice fetch-comments-camofox   --video-url 'https://www.tiktok.com/@dupflodev/video/7665295144496762134'   --ingest
```

Validated live behavior on the Dockerized Hermes Social stack: Camofox was
logged in and CAPTCHA-free, but TikTok did not render real comment containers for
the tested video; the command returned `comments: []` / `fetched: 0`, so nothing
was ingested. This is expected safe behavior until noVNC/manual interaction or a
better TikTok DOM state exposes actual comment items.

## Poll target and comment ingestion

Once video/campaign mappings are approved, `poll-targets` returns exactly the
active TikTok videos that should be scanned for comments, with only approved
campaigns and their safe public reply template.

```bash
uv run tiktok-backoffice poll-targets --limit 20
```

`ingest-comments` is the safe ingestion boundary for any reader source
(official Research API later, browser extraction, or another read-only scraper).
It imports comments for one approved video, fingerprints comments without a
platform ID, ignores duplicates, and creates review items only when the comment
text matches a campaign approved for that video.

```bash
uv run tiktok-backoffice ingest-comments \
  --video-url 'https://www.tiktok.com/@dupflodev/video/123' \
  --json-file /opt/data/tiktok-backoffice/comments-123.json
```

Accepted JSON shapes:

```json
[{"id": "comment-id", "author": "@alice", "text": "proxy"}]
```

or:

```json
{"comments": [{"author": "@alice", "text": "proxy", "created_time": 1234}]}
```

`ingest-comments` never posts. It only creates local `pending_review` items for
Hermes/Kanban/operator approval.

## Reply template rule

Campaign reply text must be the exact operator-configured message for the
resource/campaign. Never put resource links in public TikTok replies. Public
reply examples should say that the link was sent privately, e.g.
`Je viens de t’envoyer le lien en message privé`. If the intended private/public
reply flow is blocked, surface the item to Hermes/Kanban for explicit operator
approval instead of inventing a fallback.

Suggested video/campaign links:

- `suggest-video-campaigns` scans active videos with captions against active
  campaign keywords, but only trusts keywords appearing in short CTA segments like
  `Commente proxy`; generic caption mentions are ignored.
- Suggestions are stored as `source=caption_keyword`, `confidence=0.8`,
  `approved=0`; they are not used as trusted campaign mappings until approved.
- `approve-video-campaign` promotes a suggestion to `source=operator_approved`,
  `confidence=1.0`, `approved=1`.
- Manual `assign-video --source manual` remains available when the operator knows
  the exact TikTok video for a campaign.
