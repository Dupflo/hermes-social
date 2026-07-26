---
name: meta-publisher
description: Publish and schedule posts on Facebook Pages and Instagram via the Meta Graph API. Use when asked to post, schedule, or cross-post content to Facebook or Instagram.
---

# Meta Publisher

Publish content to Facebook Pages and Instagram Business accounts through the
Meta Graph API.

## Requirements

Environment variables (set in deploy/.env, never hardcoded):

- `META_PAGE_ACCESS_TOKEN` — long-lived Page access token
- `META_APP_ID` / `META_APP_SECRET` — for token refresh

## Status

Skeleton — implementation lands here. Planned capabilities:

1. Text / photo / video post to a Facebook Page
2. Instagram feed post (via connected IG Business account)
3. Scheduled publishing (`scheduled_publish_time`)
4. Post insights readback (reach, reactions)

## Notes

- Comment/DM automation is handled by the separate `meta-webhook` tool
  (see `social/tools/`), not by this skill.
