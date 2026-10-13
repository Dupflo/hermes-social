"""s15 (console ADR 042) — conditional transitions of comment_review_items.

`pending|in_review -> sending` is the claim that makes a console reply go out once; every
other writer of the table must leave a `sending` row alone. Rows are seeded through
`upsert_pending`, then their status is forced in SQL.
"""

import re
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest

from app.backfill_comments import BackfillCandidate
from app.campaign_rules import CampaignRuleStore
from app.comment_review_scanner import scan_review_candidates
from app.comment_review_store import CommentReviewItem, CommentReviewStore
from app.graph_client import GraphAPIError, GraphClient
from app.outbound_message_store import OutboundMessageStore
from app.review_comments import post_review_reply, skip_review_item
from app.store import ProcessedCommentStore
from app.webhook_manual_review import reconcile_manual_review_replies
from app.webhook_parser import CommentEvent


IG_ID = "17890000000000001"
FB_ID = "123_456"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "processed.sqlite3"
    CommentReviewStore(path)
    OutboundMessageStore(path)
    return path


@pytest.fixture
def store(db):
    return CommentReviewStore(db)


def seed(store, *, platform="instagram", comment_id=IG_ID, status="pending", parent_id=None, age=None):
    store.upsert_pending(
        CommentReviewItem(
            platform=platform,
            comment_id=comment_id,
            media_id="media-1",
            username="alice",
            text="Hello, on bosse ensemble ?",
            media_permalink=None,
            media_caption=None,
            reason="business_intent",
            score=90,
            parent_id=parent_id,
        )
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE comment_review_items SET status = ? WHERE platform = ? AND comment_id = ?",
            (status, platform, comment_id),
        )
        if age is not None:
            connection.execute(
                "UPDATE comment_review_items SET updated_at = datetime('now', ?) WHERE platform = ? AND comment_id = ?",
                (age, platform, comment_id),
            )


def dump(db, platform="instagram", comment_id=IG_ID):
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT * FROM comment_review_items WHERE platform = ? AND comment_id = ?", (platform, comment_id)
        ).fetchone()


def column(db, name, platform="instagram", comment_id=IG_ID):
    with sqlite3.connect(db) as connection:
        return connection.execute(
            f"SELECT {name} FROM comment_review_items WHERE platform = ? AND comment_id = ?", (platform, comment_id)
        ).fetchone()[0]


def outbound_rows(db):
    with sqlite3.connect(db) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM outbound_messages ORDER BY id")]


# --- claim_for_reply -------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "in_review"])
def test_claim_takes_a_pending_or_in_review_item_once(store, db, status):
    seed(store, status=status)

    assert store.claim_for_reply("instagram", IG_ID) is True
    assert column(db, "status") == "sending"
    assert store.claim_for_reply("instagram", IG_ID) is False
    assert column(db, "status") == "sending"


@pytest.mark.parametrize("status", ["replied", "manually_replied", "skipped", "ignored", "error", "blocked", "sending"])
def test_claim_refuses_any_other_status_and_leaves_the_row_identical(store, db, status):
    seed(store, status=status)
    before = dump(db)

    assert store.claim_for_reply("instagram", IG_ID) is False
    assert dump(db) == before


def test_claim_of_an_unknown_item_is_false(store):
    assert store.claim_for_reply("instagram", "1") is False


def test_two_connections_claiming_concurrently_get_exactly_one_true(db):
    first, second = CommentReviewStore(db), CommentReviewStore(db)
    seed(first)
    barrier = threading.Barrier(2)
    results = []

    def claim(s):
        barrier.wait()
        results.append(s.claim_for_reply("instagram", IG_ID))

    threads = [threading.Thread(target=claim, args=(s,)) for s in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert sorted(results) == [False, True]
    assert column(db, "status") == "sending"


# --- complete_reply --------------------------------------------------------------------


def test_complete_reply_records_the_send_and_marks_replied(store, db):
    seed(store, platform="facebook", comment_id=FB_ID)
    store.claim_for_reply("facebook", FB_ID)

    assert (
        store.complete_reply(
            "facebook",
            FB_ID,
            posted_reply_id="reply-9",
            text="  Merci, je t'écris en DM  ",
            outbound={"recipient_id": "456", "meta_response_id": "reply-9"},
        )
        is True
    )

    [sent] = outbound_rows(db)
    assert {key: sent[key] for key in sent if key not in {"id", "created_at"}} == {
        "platform": "facebook",
        "source_type": "manual_review",
        "source_id": FB_ID,
        "recipient_id": "456",
        "message_type": "public_reply",
        "message_text": "Merci, je t'écris en DM",
        "meta_response_id": "reply-9",
        "status": "sent",
    }
    assert column(db, "status", "facebook", FB_ID) == "replied"
    assert column(db, "posted_reply_id", "facebook", FB_ID) == "reply-9"
    assert column(db, "suggested_reply", "facebook", FB_ID) == "Merci, je t'écris en DM"
    assert column(db, "reviewed_at", "facebook", FB_ID) is not None


def test_complete_reply_after_a_cli_in_review_overwrite_still_marks_replied(store, db):
    seed(store)
    store.claim_for_reply("instagram", IG_ID)
    store.mark_in_review("instagram", IG_ID)  # select_next_item's unconditional write

    assert store.complete_reply(
        "instagram", IG_ID, posted_reply_id="r1", text="ok", outbound={"recipient_id": IG_ID, "meta_response_id": "r1"}
    )
    assert column(db, "status") == "replied"


def test_complete_reply_after_a_concurrent_replied_is_false_but_records_the_send(store, db):
    seed(store)
    store.claim_for_reply("instagram", IG_ID)
    store.mark_replied("instagram", IG_ID, posted_reply_id="job-reply")

    assert (
        store.complete_reply(
            "instagram", IG_ID, posted_reply_id="r1", text="ok", outbound={"recipient_id": IG_ID, "meta_response_id": "r1"}
        )
        is False
    )
    assert len(outbound_rows(db)) == 1
    assert column(db, "posted_reply_id") == "job-reply"


# --- release_after_error, skip_pending, release_stale_sending ------------------------------


def test_release_after_error_only_touches_a_sending_item(store, db):
    seed(store)
    assert store.release_after_error("instagram", IG_ID) is False
    store.claim_for_reply("instagram", IG_ID)
    assert store.release_after_error("instagram", IG_ID) is True
    assert column(db, "status") == "pending"
    assert column(db, "reason") == "business_intent"


@pytest.mark.parametrize("status", ["pending", "in_review"])
def test_skip_pending_skips_pending_and_in_review(store, db, status):
    seed(store, status=status)
    assert store.skip_pending("instagram", IG_ID) is True
    assert column(db, "status") == "skipped"
    assert column(db, "reviewed_at") is not None


@pytest.mark.parametrize("status", ["sending", "replied", "manually_replied"])
def test_skip_pending_refuses_other_statuses(store, db, status):
    seed(store, status=status)
    before = dump(db)
    assert store.skip_pending("instagram", IG_ID) is False
    assert dump(db) == before


def test_release_stale_sending_waits_two_minutes(store, db):
    seed(store, status="sending", age="-1 minutes")
    assert store.release_stale_sending("instagram", IG_ID) is False
    assert column(db, "status") == "sending"

    seed(store, status="sending", age="-3 minutes")
    assert store.release_stale_sending("instagram", IG_ID) is True
    assert column(db, "status") == "pending"


def test_release_stale_sending_ignores_a_pending_item(store, db):
    seed(store, status="pending", age="-10 minutes")
    assert store.release_stale_sending("instagram", IG_ID) is False


def test_status_of(store):
    assert store.status_of("instagram", IG_ID) is None
    seed(store, status="in_review")
    assert store.status_of("instagram", IG_ID) == "in_review"


# --- `sending` is invisible to every other writer (ADR 042 §8) ---------------------------


def test_upsert_pending_leaves_a_sending_status(store, db):
    seed(store, status="sending")
    again = CommentReviewItem(
        platform="instagram",
        comment_id=IG_ID,
        media_id="media-1",
        username="alice",
        text="texte modifié",
        media_permalink=None,
        media_caption=None,
        reason="business_intent",
        score=90,
    )
    assert store.upsert_pending(again) is False
    assert column(db, "status") == "sending"


def test_thread_reconciliation_leaves_a_sending_sibling(store, db):
    seed(store, comment_id="111", status="sending", parent_id="100")
    assert store.mark_thread_replies_manually_replied("instagram", "100", owner_reply_id="999") == 0
    assert column(db, "status", comment_id="111") == "sending"


def test_owner_webhook_echo_leaves_a_sending_item(store, db):
    seed(store, status="sending")
    event = CommentEvent(platform="instagram", comment_id="999", text="merci", parent_id=IG_ID, username="dupflodev")

    reconcile_manual_review_replies(
        events=[event], review_store=store, owner_ids=set(), owner_usernames={"dupflodev"}
    )

    assert column(db, "status") == "sending"


def test_scan_with_an_owner_reply_leaves_a_sending_item(store, db):
    seed(store, status="sending")
    scan_review_candidates(
        comments=[
            BackfillCandidate(
                platform="instagram",
                comment_id=IG_ID,
                text="Hello, on bosse ensemble ?",
                media_id="media-1",
                username="alice",
                has_owner_reply=True,
                owner_reply_id="999",
            )
        ],
        media_contexts={},
        rule_store=CampaignRuleStore(db),
        processed_store=ProcessedCommentStore(db),
        review_store=store,
    )
    assert column(db, "status") == "sending"


def test_meta_backfill_selection_only_reads_pending_and_pending_review():
    # Pin (console ADR 042 §8): if meta_cron_verify.py ever selects `sending`, the
    # console's claim no longer protects the item from the job.
    source = (Path(__file__).resolve().parents[1] / "app" / "meta_cron_verify.py").read_text()
    start = source.index("async def process_pending_keyword_items")
    selection = source[start : source.index("LIMIT 300", start)]
    assert re.search(r"WHERE status IN \('pending','pending_review'\)", selection)
    assert "sending" not in selection


# --- GraphAPIError carries the raw Meta message and the HTTP status ----------------------


async def _graph_error(response: httpx.Response) -> GraphAPIError:
    transport = httpx.MockTransport(lambda request: response)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = GraphClient(access_token="page-token", http_client=http_client)
        with pytest.raises(GraphAPIError) as info:
            await client.reply_to_instagram_comment("1", "x")
    return info.value


@pytest.mark.asyncio
async def test_graph_error_exposes_meta_message_and_http_status():
    error = await _graph_error(
        httpx.Response(400, json={"error": {"message": "(#100) x", "code": 100, "error_subcode": 33}})
    )
    assert error.meta_message == "(#100) x"
    assert error.http_status == 400
    assert error.code == 100
    assert error.subcode == 33
    assert str(error) == "Meta Graph API error 100/33: (#100) x"


@pytest.mark.asyncio
async def test_graph_error_on_an_html_page_has_no_meta_message():
    error = await _graph_error(httpx.Response(502, text="<html>Bad gateway</html>"))
    assert error.meta_message is None
    assert error.code is None
    assert error.http_status == 502


def test_graph_error_defaults_keep_existing_constructors_working():
    error = GraphAPIError("boom", code=1)
    assert error.meta_message is None
    assert error.http_status is None


# --- historical CLI refuses a `sending` item ---------------------------------------------


class RecordingGraph:
    def __init__(self):
        self.calls = []

    async def reply_to_instagram_comment(self, comment_id, message):
        self.calls.append(("instagram", comment_id))
        return {"id": "x"}

    async def reply_to_facebook_comment(self, comment_id, message):
        self.calls.append(("facebook", comment_id))
        return {"id": "x"}

    async def send_direct_message(self, recipient_id, text):
        self.calls.append(("dm", recipient_id))
        return {"message_id": "x"}


@pytest.mark.asyncio
async def test_cli_reply_refuses_a_sending_item_without_calling_graph(store, db):
    seed(store, status="sending")
    graph = RecordingGraph()

    message = await post_review_reply(store, graph, platform="instagram", comment_id=IG_ID, text="ok")

    assert message == "Envoi en cours depuis la console."
    assert graph.calls == []
    assert column(db, "status") == "sending"


def test_cli_skip_refuses_a_sending_item(store, db):
    seed(store, status="sending")

    assert skip_review_item(store, "instagram", IG_ID) == "Envoi en cours depuis la console."
    assert column(db, "status") == "sending"
