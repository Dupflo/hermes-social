# Comment Review Telegram Queue Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build a SQLite-backed queue of interesting Facebook/Instagram comments so Florian can process them one by one from Telegram without opening Meta apps.

**Architecture:** Reuse the existing Meta Graph API fetcher/webhook parser to ingest comments into a new `comment_review_items` table. A scanner classifies only comments worth human attention, stores them with status `pending`, and exposes CLI commands to show the next item, post a reply, skip, or mark done. Hermes/Telegram can call these CLI commands when Florian asks “y a-t-il des commentaires intéressants à traiter ?”.

**Tech Stack:** FastAPI app, Python 3.13, SQLite, existing `httpx` Graph API client, existing `uv`/pytest setup.

---

## Current context

Existing relevant files:

- `app/backfill_comments.py` already fetches Facebook + Instagram comments across media.
- `app/graph_client.py` already posts public replies and private replies.
- `app/store.py` owns `processed_comments` state.
- `app/campaign_rules.py` owns automated keyword campaigns.
- `app/webhook_parser.py` parses webhook comment events.
- Tests live under `tests/`.

Existing behavior:

- Keyword automation handles resource comments.
- Comments matching active campaigns are processed automatically.
- Facebook private-reply blocked comments get a fallback public reply.

New behavior wanted:

- Do **not** show every comment.
- Store only comments that look useful to handle manually.
- When Florian asks from Telegram, return one comment at a time.
- Florian replies in Telegram.
- Hermes posts Florian’s reply directly to the original Facebook/Instagram comment.
- Then move to the next comment.
- If Florian replies manually from Facebook/Instagram, the webhook/next scan must detect the owner reply and update SQLite so Hermes does not show or retry that comment.
- If Florian asks for the original comment link, Hermes must return a direct/best-effort platform URL to open it quickly.

Assumption:

- “Sky Light” means **SQLite**.

---

## Proposed data model

Create a new SQLite table, probably in the same DB `data/processed_comments.sqlite3`:

```sql
CREATE TABLE IF NOT EXISTS comment_review_items (
    platform TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    media_id TEXT,
    username TEXT,
    text TEXT NOT NULL,
    media_permalink TEXT,
    media_caption TEXT,
    reason TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    suggested_reply TEXT,
    posted_reply_id TEXT,
    comment_permalink TEXT,
    owner_replied_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    PRIMARY KEY(platform, comment_id)
);
```

Status values:

- `pending`: needs Florian review.
- `in_review`: currently shown to Florian.
- `replied`: Hermes posted Florian’s reply.
- `manually_replied`: Florian/the page replied directly on Facebook/Instagram and the scanner/webhook detected it.
- `skipped`: Florian chose not to answer.
- `ignored`: scanner classified as not useful anymore.
- `error`: attempted reply failed.

---

## Classification rules

The scanner should add comments only if they are not already automated/processed and match one of these categories:

1. **Direct question / request**
   - Contains: `lien`, `link`, `ressource`, `outil`, `nom`, `site`, `repo`, `guide`, `comment faire`, `tu peux`, `peux-tu`, `stp`, `svp`, `intéressé`, `interessé`, `DM`, `MP`.

2. **Complaint / automation failure**
   - Contains: `j'ai rien reçu`, `tu nous envoies que dalle`, `pas reçu`, `pas de dm`, `ça marche pas`, `lien ?`.

3. **Business / collaboration intent**
   - Contains: `travailler avec toi`, `message privé`, `collab`, `projet`, `client`, `beta testeur`.

4. **Short potential keyword not configured**
   - 1–3 words, not common praise/noise, and not already matching an active campaign.
   - Examples found during audit: `Repo`, `Rpi`, `#OpenDesign`, `Intéressé`, `Le nom du site svp`, `Le nom du mcp`.

Noise to avoid storing:

- `merci`, `top`, `bravo`, `cool`, emoji-only, generic praise with no actionable request.
- Comments from the page/account itself.
- Comments already having owner reply unless they contain a complaint or were not marked reviewed.
- Comments matching an active automated campaign unless automation failed and needs manual follow-up.

---

## Task 1: Add review queue store

**Objective:** Add a dedicated store for human-review comments.

**Files:**

- Create: `app/comment_review_store.py`
- Test: `tests/test_comment_review_store.py`

**Implementation outline:**

Create dataclass:

```python
@dataclass(frozen=True)
class CommentReviewItem:
    platform: str
    comment_id: str
    media_id: str | None
    username: str | None
    text: str
    media_permalink: str | None
    media_caption: str | None
    reason: str
    score: int
    status: str = "pending"
    suggested_reply: str | None = None
    comment_permalink: str | None = None
```

Store methods:

- `upsert_pending(item: CommentReviewItem) -> bool`
- `next_pending() -> CommentReviewItem | None`
- `mark_in_review(platform, comment_id)`
- `mark_replied(platform, comment_id, posted_reply_id)`
- `mark_manually_replied(platform, comment_id, owner_reply_id=None, owner_replied_at=None)`
- `mark_skipped(platform, comment_id)`
- `mark_error(platform, comment_id, error_message)`
- `counts_by_status() -> dict[str, int]`

**Tests:**

- Inserts a pending item.
- Duplicate upsert does not reset a replied item to pending.
- `next_pending()` returns highest score then oldest created.
- `mark_replied()` stores reply id and status.
- `mark_manually_replied()` stores owner reply metadata and prevents the item from returning in `next_pending()`.

Run:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/meta-comment-dm-automation-test-venv uv run pytest tests/test_comment_review_store.py -q
```

---

## Task 2: Add comment classifier

**Objective:** Decide which comments are worth manual review.

**Files:**

- Create: `app/comment_review_classifier.py`
- Test: `tests/test_comment_review_classifier.py`

**Implementation outline:**

Create:

```python
def classify_comment_for_review(
    *,
    text: str,
    has_owner_reply: bool,
    matches_active_campaign: bool,
    already_terminal: bool,
) -> tuple[bool, str, int]:
    ...
```

Return:

- `(True, "direct_request", 80)`
- `(True, "automation_complaint", 100)`
- `(True, "business_intent", 90)`
- `(True, "possible_unconfigured_keyword", 40)`
- `(False, "noise", 0)`

**Tests:**

- `Le nom du site svp` => direct request.
- `C'est nulle tes tuto lorsqu'on commente tu nous envoi que dalle` => automation complaint.
- `je t'envoie un message privé` => business intent.
- `Repo` => possible unconfigured keyword.
- `Merci !` => noise.
- Active campaign + already terminal => ignored unless complaint.

---

## Task 3: Build scanner CLI

**Objective:** Scan Facebook/Instagram comments and populate `comment_review_items`.

**Files:**

- Create: `app/scan_comment_reviews.py`
- Modify: maybe `app/backfill_comments.py` to expose media metadata fetch cleanly.
- Test: `tests/test_scan_comment_reviews.py`

CLI command:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/meta-comment-dm-automation-test-venv uv run python -m app.scan_comment_reviews --platform all --media-limit 200 --comments-limit 1000
```

Output:

```text
scanned_comments=1174
inserted_pending=12
already_known=49
ignored=1113
pending_total=12
```

Scanner behavior:

1. Fetch Facebook and Instagram comments with media metadata.
2. For each comment:
   - Build `CommentEvent`.
   - Check `CampaignRuleStore.find_matching_rule(event)`.
   - Check `ProcessedCommentStore` terminal/processed states.
   - Classify using `comment_review_classifier`.
   - Upsert if relevant.
   - If an existing pending/in_review item now has an owner reply, mark it `manually_replied` instead of leaving it pending.
3. Do not post anything.

Owner reply detection:

- Facebook comments already expose nested `comments.limit(25){id,message,from,created_time}` in `MetaCommentFetcher.fetch_facebook_comments()`.
- Instagram comments already expose nested `replies{id,text,username,timestamp}` in `MetaCommentFetcher.fetch_instagram_comments()`.
- Reuse `has_owner_reply`; optionally extend `BackfillCandidate` with `owner_reply_id` and `owner_reply_at` for auditability.
- This scanner should be safe to run periodically: it reconciles external manual replies back into SQLite.

---

## Task 4: Add review queue CLI for Telegram/Hermes use

**Objective:** Provide simple commands Hermes can call from Telegram.

**Files:**

- Create: `app/review_comments.py`
- Test: `tests/test_review_comments.py`

Commands:

```bash
python -m app.review_comments count
python -m app.review_comments next
python -m app.review_comments link --platform facebook --comment-id '...'
python -m app.review_comments reply --platform facebook --comment-id '...' --text '...'
python -m app.review_comments skip --platform facebook --comment-id '...'
```

`next` output should be Telegram-friendly:

```text
1 commentaire intéressant à traiter.

Plateforme: Facebook
Auteur: unknown
Vidéo: https://www.facebook.com/reel/...
Raison: demande directe
Commentaire:
"Le nom du site svp"

Réponds directement avec le message à poster, ou dis "skip".
Si tu veux ouvrir le commentaire sur la plateforme, dis "lien".
```

Important:

- `next` should mark the item `in_review` so repeated calls don’t show a different item mid-conversation.
- If there is already an `in_review` item, show that same item until replied/skipped.
- `link` should return `comment_permalink` when available, otherwise build a best-effort URL from the media permalink + comment ID.

Direct comment link strategy:

- Store `media_permalink` for every review item.
- Store `comment_permalink` if Graph API returns a usable field for the comment.
- If no direct comment permalink exists:
  - Facebook: return the Reel/post permalink plus the comment ID as context.
  - Instagram: return the Reel permalink plus the comment ID/username as context.
- Telegram response should be practical:

```text
Lien vidéo/commentaire : https://www.facebook.com/reel/...
Comment ID : 123_456
Auteur : alice
```

---

## Task 5: Posting manual replies

**Objective:** Post Florian’s Telegram reply back to the source comment.

**Files:**

- Modify: `app/graph_client.py` if needed.
- Modify/Create: `app/review_comments.py`.
- Test: `tests/test_review_comments.py`.

Posting logic:

- Facebook:
  - Public reply endpoint: `/{short_comment_id}/comments`.
  - For compound IDs like `post_comment`, use the short comment ID for public reply, same as `CommentProcessor._graph_action_comment_id`.
- Instagram:
  - Public reply endpoint: `/{comment_id}/replies`.

After successful post:

- Store `posted_reply_id`.
- Mark status `replied`.
- Print confirmation.

Manual reply reconciliation:

- If Florian posts a reply manually on Meta, the next webhook/scan must detect the page/owner reply and call `mark_manually_replied()`.
- If the item is currently `in_review`, it should be removed from the active review flow and the next `review_comments next` should show the next pending item.
- If Hermes tries to reply to an item that became `manually_replied`, it should refuse with a clear message: `Déjà répondu manuellement sur la plateforme.`

If posting fails:

- Mark status `error`.
- Print the Meta error.

---

## Task 6: Hermes interaction workflow

**Objective:** Define how Florian will use it from Telegram.

No code may be needed if Hermes manually invokes the CLI tools, but document the flow in README.

Desired chat flow:

1. Florian: `Y a-t-il des commentaires intéressants à traiter ?`
2. Hermes runs:
   ```bash
   python -m app.scan_comment_reviews --platform all --media-limit 200 --comments-limit 1000
   python -m app.review_comments next
   ```
3. Hermes shows exactly one comment.
4. Florian replies with text.
5. Hermes runs:
   ```bash
   python -m app.review_comments reply --platform ... --comment-id ... --text '...'
   ```
6. Hermes confirms and offers next comment.
7. If Florian says `lien`, Hermes runs:
   ```bash
   python -m app.review_comments link --platform ... --comment-id ...
   ```
   and returns the platform URL/context.
8. If Florian answered manually on Meta, the next scan/webhook marks the item `manually_replied` and Hermes moves on.

Optional later improvement:

- A cron job can run `scan_comment_reviews` every 15–30 minutes and stay silent unless `pending_total > 0`.

---

## Task 7: Documentation

**Objective:** Document usage and safety rules.

**Files:**

- Modify: `README.md`

Add section:

```markdown
## Manual comment review queue

Use this when comments are interesting but should not be answered automatically.

Scan:
...

Show next:
...

Reply:
...

Skip:
...
```

Safety notes:

- Scanner never posts replies.
- Replies are posted only after Florian writes the response in Telegram.
- One comment at a time to avoid mistakes.
- Keep automated resource campaigns separate from manual review queue.
- Manual replies made directly on Facebook/Instagram are reconciled back into SQLite before showing the queue again.
- Always provide a platform link/context on request so Florian can quickly verify the comment in Meta.

---

## Validation checklist

Before deployment:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/meta-comment-dm-automation-test-venv uv run pytest -q
```

Manual dry-run:

```bash
UV_PROJECT_ENVIRONMENT=/tmp/meta-comment-dm-automation-test-venv uv run python -m app.scan_comment_reviews --platform all --media-limit 20 --comments-limit 50
UV_PROJECT_ENVIRONMENT=/tmp/meta-comment-dm-automation-test-venv uv run python -m app.review_comments count
UV_PROJECT_ENVIRONMENT=/tmp/meta-comment-dm-automation-test-venv uv run python -m app.review_comments next
```

Deployment:

```bash
touch /opt/data/meta-comment-dm-automation/.deploy-trigger
```

Verify:

```bash
curl -fsS http://127.0.0.1:8791/health
```

---

## Open questions

1. Should manual replies be public comment replies only, or should Hermes also offer private DM when Meta allows it?
2. Should the scanner include comments with owner replies, or ignore them by default?
3. Should we store only one active `in_review` item globally, or one per Telegram chat/user?
4. Should we add a daily/periodic cron scan later?
5. Which exact Graph API field/URL format gives the most reliable direct comment permalink for Facebook Reels and Instagram comments?

Recommended defaults:

- Start with public replies only.
- Ignore comments already answered by the owner unless they look like complaints.
- Use one global `in_review` item, because Florian is the only operator for now.
- Add cron later, after manual flow is validated.
- Store media permalink always; store direct comment permalink when available; otherwise return media permalink + comment ID.
