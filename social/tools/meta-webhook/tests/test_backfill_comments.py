import httpx
import pytest

from app.backfill_comments import BackfillCandidate, MetaCommentFetcher, dedupe_comments, find_backfill_candidates, filter_by_comment_ids
from app.campaign_rules import CampaignRule, CampaignRuleStore
from app.store import ProcessedCommentStore


@pytest.mark.asyncio
async def test_fetch_instagram_comments_from_recent_media():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/ig-user-1/media"):
            return httpx.Response(200, json={"data": [{"id": "media-1"}]})
        if request.url.path.endswith("/media-1/comments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "comment-1",
                            "text": "Proxy svp",
                            "username": "alice",
                            "replies": {"data": []},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
        )
        comments = await fetcher.fetch_instagram_comments("ig-user-1", media_limit=1, comments_limit=10)

    assert len(comments) == 1
    assert comments[0].platform == "instagram"
    assert comments[0].comment_id == "comment-1"
    assert comments[0].text == "Proxy svp"
    assert comments[0].media_id == "media-1"
    assert comments[0].username == "alice"
    assert comments[0].has_owner_reply is False
    assert any("access_token=token" in url for url in requests)


@pytest.mark.asyncio
async def test_fetch_instagram_comments_paginates_with_small_graph_pages():
    comment_pages = [
        {
            "data": [{"id": "comment-1", "text": "Proxy", "username": "alice"}],
            "paging": {"next": "https://graph.facebook.com/v21.0/media-1/comments?page=2"},
        },
        {
            "data": [{"id": "comment-2", "text": "Proxy", "username": "bob"}],
        },
    ]
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path.endswith("/ig-user-1/media"):
            return httpx.Response(200, json={"data": [{"id": "media-1"}]})
        if request.url.path.endswith("/media-1/comments"):
            return httpx.Response(200, json=comment_pages.pop(0))
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
            page_size=25,
        )
        comments = await fetcher.fetch_instagram_comments("ig-user-1", media_limit=1, comments_limit=100)

    assert [comment.comment_id for comment in comments] == ["comment-1", "comment-2"]
    assert "limit=25" in requested_urls[1]
    assert requested_urls[2] == "https://graph.facebook.com/v21.0/media-1/comments?page=2"


@pytest.mark.asyncio
async def test_fetch_instagram_comments_marks_existing_owner_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ig-user-1/media"):
            return httpx.Response(200, json={"data": [{"id": "media-1"}]})
        if request.url.path.endswith("/media-1/comments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "comment-1",
                            "text": "Proxy",
                            "username": "alice",
                            "replies": {"data": [{"username": "dupflodev", "text": "C'est envoyé"}]},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
            owner_usernames={"dupflodev"},
        )
        comments = await fetcher.fetch_instagram_comments("ig-user-1", media_limit=1, comments_limit=10)

    assert comments[0].has_owner_reply is True


@pytest.mark.asyncio
async def test_fetch_instagram_comments_includes_non_owner_thread_replies():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ig-user-1/media"):
            return httpx.Response(200, json={"data": [{"id": "media-1"}]})
        if request.url.path.endswith("/media-1/comments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "comment-1",
                            "text": "Repo ?",
                            "username": "alice",
                            "replies": {
                                "data": [
                                    {"id": "reply-owner", "text": "Voilà", "username": "dupflodev"},
                                    {"id": "reply-user", "text": "Comment on l'installe ?", "username": "alice"},
                                ]
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
            owner_usernames={"dupflodev"},
        )
        comments = await fetcher.fetch_instagram_comments("ig-user-1", media_limit=1, comments_limit=10)

    assert [comment.comment_id for comment in comments] == ["comment-1", "reply-user"]
    assert comments[1].parent_id == "comment-1"
    assert comments[1].text == "Comment on l'installe ?"


def test_find_backfill_candidates_keeps_matching_unprocessed_comments(tmp_path):
    rule_store = CampaignRuleStore(tmp_path / "app.sqlite3")
    processed_store = ProcessedCommentStore(tmp_path / "app.sqlite3")
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="proxy",
            name="Proxy",
            platform="any",
            media_id=None,
            keywords=["proxy", "proxi"],
            public_reply_text="C'est envoyé check tes DMs !",
            dm_text="DM proxy",
            enabled=True,
        )
    )
    processed_store.mark_processed(
        platform="instagram",
        comment_id="already-done",
        keyword="proxy",
        like_sent=False,
        public_reply_sent=True,
        dm_sent=True,
    )

    candidates = find_backfill_candidates(
        comments=[
            BackfillCandidate(platform="instagram", comment_id="todo", text="Proxi svp", media_id="media-1", username="alice"),
            BackfillCandidate(platform="instagram", comment_id="ignored", text="Hello", media_id="media-1", username="bob"),
            BackfillCandidate(platform="instagram", comment_id="already-done", text="Proxy", media_id="media-1", username="carol"),
            BackfillCandidate(platform="instagram", comment_id="already-replied", text="Proxy", media_id="media-1", username="dan", has_owner_reply=True),
        ],
        rule_store=rule_store,
        processed_store=processed_store,
    )

    assert candidates == [
        BackfillCandidate(platform="instagram", comment_id="todo", text="Proxi svp", media_id="media-1", username="alice")
    ]


def test_find_backfill_candidates_keeps_failed_comments_retryable(tmp_path):
    rule_store = CampaignRuleStore(tmp_path / "app.sqlite3")
    processed_store = ProcessedCommentStore(tmp_path / "app.sqlite3")
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="proxy",
            name="Proxy",
            platform="any",
            media_id=None,
            keywords=["proxy"],
            public_reply_text="C'est envoyé check tes DMs !",
            dm_text="DM proxy",
            enabled=True,
        )
    )
    processed_store.mark_failed(platform="facebook", comment_id="failed-comment", keyword="proxy")

    candidates = find_backfill_candidates(
        comments=[BackfillCandidate(platform="facebook", comment_id="failed-comment", text="Proxy")],
        rule_store=rule_store,
        processed_store=processed_store,
    )

    assert candidates == [BackfillCandidate(platform="facebook", comment_id="failed-comment", text="Proxy")]


def test_find_backfill_candidates_keeps_partially_processed_comments_retryable(tmp_path):
    rule_store = CampaignRuleStore(tmp_path / "app.sqlite3")
    processed_store = ProcessedCommentStore(tmp_path / "app.sqlite3")
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="proxy",
            name="Proxy",
            platform="any",
            media_id=None,
            keywords=["proxy"],
            public_reply_text="C'est envoyé check tes DMs !",
            dm_text="DM proxy",
            enabled=True,
        )
    )
    processed_store.mark_processed(
        platform="facebook",
        comment_id="missing-public-reply",
        keyword="proxy",
        like_sent=False,
        public_reply_sent=False,
        dm_sent=True,
    )

    candidates = find_backfill_candidates(
        comments=[BackfillCandidate(platform="facebook", comment_id="missing-public-reply", text="Proxy")],
        rule_store=rule_store,
        processed_store=processed_store,
    )

    assert candidates == [BackfillCandidate(platform="facebook", comment_id="missing-public-reply", text="Proxy")]


def test_find_backfill_candidates_keeps_owner_replied_comment_when_dm_is_missing(tmp_path):
    rule_store = CampaignRuleStore(tmp_path / "app.sqlite3")
    processed_store = ProcessedCommentStore(tmp_path / "app.sqlite3")
    rule_store.upsert_rule(
        CampaignRule(
            source_task_id="proxy",
            name="Proxy",
            platform="any",
            media_id=None,
            keywords=["proxy"],
            public_reply_text="C'est envoyé check tes DMs !",
            dm_text="DM proxy",
            enabled=True,
        )
    )
    processed_store.mark_processed(
        platform="facebook",
        comment_id="missing-dm",
        keyword="proxy",
        like_sent=True,
        public_reply_sent=True,
        dm_sent=False,
    )

    candidates = find_backfill_candidates(
        comments=[
            BackfillCandidate(
                platform="facebook",
                comment_id="missing-dm",
                text="Proxy",
                has_owner_reply=True,
            )
        ],
        rule_store=rule_store,
        processed_store=processed_store,
    )

    assert candidates == [
        BackfillCandidate(platform="facebook", comment_id="missing-dm", text="Proxy", has_owner_reply=True)
    ]


def test_filter_by_comment_ids_keeps_requested_ids_only():
    comments = [
        BackfillCandidate(platform="instagram", comment_id="keep", text="Proxy"),
        BackfillCandidate(platform="instagram", comment_id="skip", text="Proxy"),
    ]

    assert filter_by_comment_ids(comments, {"keep"}) == [comments[0]]
    assert filter_by_comment_ids(comments, set()) == comments


def test_dedupe_comments_keeps_first_platform_comment_pair():
    comments = [
        BackfillCandidate(platform="facebook", comment_id="same", text="Proxy", media_id="post-1"),
        BackfillCandidate(platform="facebook", comment_id="same", text="Proxy", media_id="post-1"),
        BackfillCandidate(platform="instagram", comment_id="same", text="Proxy", media_id="media-1"),
    ]

    assert dedupe_comments(comments) == [comments[0], comments[2]]


@pytest.mark.asyncio
async def test_fetch_facebook_comments_from_recent_posts():
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path.endswith("/page-1/posts"):
            return httpx.Response(200, json={"data": [{"id": "post-1"}]})
        if request.url.path.endswith("/post-1/comments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "fb-comment-1",
                            "message": "Magic",
                            "from": {"name": "Alice"},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
        )
        comments = await fetcher.fetch_facebook_comments("page-1", post_limit=1, comments_limit=10)

    assert comments == [
        BackfillCandidate(
            platform="facebook",
            comment_id="fb-comment-1",
            text="Magic",
            media_id="post-1",
            username="Alice",
        )
    ]
    assert "comments.limit%28100%29" in requested_urls[1]


@pytest.mark.asyncio
async def test_fetch_facebook_comments_marks_existing_page_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/page-1/posts"):
            return httpx.Response(200, json={"data": [{"id": "post-1"}]})
        if request.url.path.endswith("/post-1/comments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "fb-comment-1",
                            "message": "Proxy",
                            "from": {"name": "Alice", "id": "user-1"},
                            "comments": {
                                "data": [
                                    {
                                        "id": "reply-1",
                                        "message": "C'est envoyé",
                                        "from": {"name": "FloDev", "id": "page-1"},
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
            owner_ids={"page-1"},
        )
        comments = await fetcher.fetch_facebook_comments("page-1", post_limit=1, comments_limit=10)

    assert comments[0].has_owner_reply is True


@pytest.mark.asyncio
async def test_fetch_facebook_comments_includes_non_owner_thread_replies():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/page-1/posts"):
            return httpx.Response(200, json={"data": [{"id": "post-1"}]})
        if request.url.path.endswith("/post-1/comments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "fb-comment-1",
                            "message": "Repo ?",
                            "from": {"name": "Alice", "id": "user-1"},
                            "comments": {
                                "data": [
                                    {
                                        "id": "reply-owner",
                                        "message": "Voilà",
                                        "from": {"name": "FloDev", "id": "page-1"},
                                    },
                                    {
                                        "id": "reply-user",
                                        "message": "Ça marche comment ?",
                                        "from": {"name": "Alice", "id": "user-1"},
                                    },
                                ]
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
            owner_ids={"page-1"},
        )
        comments = await fetcher.fetch_facebook_comments("page-1", post_limit=1, comments_limit=10)

    assert [comment.comment_id for comment in comments] == ["fb-comment-1", "reply-user"]
    assert comments[1].parent_id == "fb-comment-1"
    assert comments[1].text == "Ça marche comment ?"


@pytest.mark.asyncio
async def test_fetch_instagram_thread_reply_marks_later_owner_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ig-user-1/media"):
            return httpx.Response(200, json={"data": [{"id": "media-1"}]})
        if request.url.path.endswith("/media-1/comments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "comment-1",
                            "text": "Repo ?",
                            "username": "alice",
                            "replies": {
                                "data": [
                                    {"id": "reply-user", "text": "Et ensuite ?", "username": "alice", "timestamp": "2026-07-25T10:00:00+0000"},
                                    {"id": "reply-owner", "text": "Voilà", "username": "dupflodev", "timestamp": "2026-07-25T10:01:00+0000"},
                                ]
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = MetaCommentFetcher(
            access_token="token",
            http_client=client,
            api_version="v21.0",
            owner_usernames={"dupflodev"},
        )
        comments = await fetcher.fetch_instagram_comments("ig-user-1", media_limit=1, comments_limit=10)

    reply = comments[1]
    assert reply.comment_id == "reply-user"
    assert reply.parent_id == "comment-1"
    assert reply.has_owner_reply is True
    assert reply.owner_reply_id == "reply-owner"
    assert reply.owner_reply_at == "2026-07-25T10:01:00+0000"
