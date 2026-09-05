from app.webhook_parser import CommentEvent, parse_comment_events, parse_private_message_events


def test_parse_instagram_comment_event_from_changes_payload():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "ig-user-1",
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": "comment-1",
                            "media_id": "media-1",
                            "text": "Proxy svp",
                            "from": {"id": "user-1", "username": "alice"},
                        },
                    }
                ],
            }
        ],
    }

    assert parse_comment_events(payload) == [
        CommentEvent(
            platform="instagram",
            comment_id="comment-1",
            text="Proxy svp",
            media_id="media-1",
            author_id="user-1",
            username="alice",
        )
    ]


def test_parse_page_feed_comment_event_from_changes_payload():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "add",
                            "comment_id": "fb-comment-1",
                            "post_id": "post-1",
                            "message": "proxy",
                            "from": {"id": "user-1", "name": "Alice"},
                        },
                    }
                ],
            }
        ],
    }

    assert parse_comment_events(payload) == [
        CommentEvent(
            platform="facebook",
            comment_id="fb-comment-1",
            text="proxy",
            media_id="post-1",
            author_id="user-1",
            username="Alice",
        )
    ]


def test_parser_ignores_non_comment_events():
    payload = {"object": "page", "entry": [{"changes": [{"field": "feed", "value": {"item": "post"}}]}]}

    assert parse_comment_events(payload) == []


def test_parse_page_feed_ignores_non_add_comment_verbs():
    payload = {
        "object": "page",
        "entry": [
            {
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": "edited",
                            "comment_id": "fb-comment-1",
                            "post_id": "post-1",
                            "message": "proxy",
                            "from": {"id": "user-1", "name": "Alice"},
                        },
                    }
                ]
            }
        ],
    }

    assert parse_comment_events(payload) == []


def test_parse_instagram_quick_reply_payload():
    payload = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": "igsid-1"},
                        "recipient": {"id": "ig-business"},
                        "timestamp": 1,
                        "message": {
                            "mid": "mid-1",
                            "text": "✅ J’ai suivi",
                            "quick_reply": {"payload": "FOLLOW_GATE_CONFIRMED"},
                        },
                    }
                ]
            }
        ],
    }

    events = parse_private_message_events(payload)

    assert len(events) == 1
    assert events[0].platform == "instagram_dm"
    assert events[0].sender_id == "igsid-1"
    assert events[0].text == "✅ J’ai suivi"
    assert events[0].payload == "FOLLOW_GATE_CONFIRMED"
