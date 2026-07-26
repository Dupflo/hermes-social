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

uv run tiktok-backoffice draft --comment-id comment-1 --keyword proxy   --reply 'Envoie-moi “proxy” en DM et je te l’envoie 👍'

uv run tiktok-backoffice next
uv run tiktok-backoffice list
```

`draft` records a draft locally and returns the exact text to review. It does
not click, type, or post in TikTok.

## Future browser-assisted step

When Camofox is available, the next incremental command should be a separate
`browser-draft` action that opens TikTok with Hermes/Camofox, finds the comment,
puts the reply text in the reply box, captures evidence, and stops before the
Publish/Post action.
