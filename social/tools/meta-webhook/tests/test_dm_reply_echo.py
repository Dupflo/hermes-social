"""s17 (console ADR 045 §10, AC 5) — an echo of the page's own message never unlocks a follow-gate.

The console only sends as the page. If Meta ever delivers the echo of such a message (`is_echo`,
`sender.id` = the page or its Instagram account), its sender is never the `sender_id` of a pending
gate: the unlock path (unchanged by s17) keys on the incoming sender, not on the text.
"""

import pytest

from app.follow_gate_store import FollowGateStore
from app.outbound_message_store import OutboundMessageStore
from app.processor import PrivateMessageProcessor
from app.store import ProcessedCommentStore
from app.webhook_parser import parse_private_message_events

PAGE_ID = "999"
IG_USER_ID = "17841400000000000"
LEAD = "lead-1"


class RecordingGraph:
    def __init__(self):
        self.calls = []

    async def private_reply_to_instagram_comment(self, page_id, comment_id, text, quick_replies=None):
        self.calls.append(("private_reply", page_id, comment_id))
        return {"message_id": "m"}


def _echo_payload(object_name, owner_id, text):
    return {
        "object": object_name,
        "entry": [
            {
                "id": owner_id,
                "messaging": [
                    {
                        "sender": {"id": owner_id},
                        "recipient": {"id": LEAD},
                        "timestamp": 1791795960000,
                        "message": {"mid": "m_echo_1", "text": text, "is_echo": True},
                    }
                ],
            }
        ],
    }


def _outbound(database):
    import sqlite3

    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT COUNT(*) FROM outbound_messages").fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("object_name", "owner_id", "gate_platform"),
    [("page", PAGE_ID, "facebook"), ("instagram", IG_USER_ID, "instagram")],
)
@pytest.mark.parametrize("text", ["✅ J'ai suivi", "✅ J’ai suivi", "ok"])
async def test_a_page_echo_never_unlocks_a_pending_gate(tmp_path, object_name, owner_id, gate_platform, text):
    database = tmp_path / "processed.sqlite3"
    gates = FollowGateStore(database)
    gates.create_pending(
        platform=gate_platform,
        comment_id="comment-1",
        sender_id=LEAD,
        campaign="Guide",
        keyword="guide",
        dm_text="Voici le lien",
    )
    outbound = OutboundMessageStore(database)
    graph = RecordingGraph()
    processor = PrivateMessageProcessor(
        graph_client=graph,
        follow_gate_store=gates,
        processed_store=ProcessedCommentStore(database),
        outbound_store=outbound,
        page_id=PAGE_ID,
    )

    events = parse_private_message_events(_echo_payload(object_name, owner_id, text))
    assert [event.sender_id for event in events] == [owner_id]
    results = await processor.process_events(events)

    assert [result.status for result in results] != ["follow_gate_unlocked"]
    assert results[0].status == "no_pending_follow_gate"
    assert graph.calls == []
    assert gates.pending_for_sender(gate_platform, LEAD) is not None
    assert _outbound(database) == 0
