"""s09 — internal listener for the console (served on :8792 by app/serve.py, never published).

Console ADR 012 (Python stays the source of truth, /internal/* on a second app) and ADR 031
(replay contract). Every route requires `Authorization: Bearer <CONSOLE_INTERNAL_TOKEN>`;
an empty server token refuses everything. Responses carry codes and statuses only: never
the comment text, an error message, a username or a Graph URL.

s13 (console ADR 038): `POST /internal/keywords/match` previews a campaign's keywords with
`app.keyword.contains_keyword` (the very matcher of the webhook), so the console never
reimplements the Meta logic. Pure function: no database, no Graph call, index only.

s15 (console ADR 042): `POST /internal/review/{platform}/{comment_id}/reply|skip|release` treat
the review queue. A reply goes to Graph only after the conditional claim `pending|in_review ->
sending`; a certain Meta refusal puts the item back `pending`, anything ambiguous leaves it
`sending` (`uncertain`), never a 500. Bounded exception to "codes only": the `graph_error` result of
the `reply` route carries Meta's raw `error.message` (page token redacted, 300 characters max).
No other text — comment, reply, username, Graph URL — ever appears in a response or a log.
"""

import base64
import binascii
import hmac
import json
import logging
import time
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import replay
from app.comment_review_store import CommentReviewStore
from app.config import Settings, get_settings
from app.graph_client import GraphAPIError, GraphClient
from app.keyword import contains_keyword
from app.outbound_message_store import OutboundMessageStore
from app.platform_utils import facebook_graph_comment_id
from app.processor import CommentProcessor
from app.processor_factory import build_comment_processor
from app.store import ProcessedCommentStore


logger = logging.getLogger(__name__)

COMMENT_ID_PATTERN = r"^[0-9]{1,40}(_[0-9]{1,40})?$"
BATCH_SIZE = 20
BATCH_TIME_BUDGET_S = 90
GRAPH_TIMEOUT_S = 10
REVIEW_TEXT_MAX = 2000
META_MESSAGE_MAX = 300
RELEASE_AFTER_MINUTES = 2
TRANSIENT_META_CODES = {1, 2}

CLOSE_REASONS = {
    "window_expired": "clos manuellement, fenêtre Meta dépassée",
    "ignored": "clos manuellement, aucune campagne ne correspond",
    "comment_unavailable": "clos manuellement, commentaire introuvable chez Meta",
}

BATCH_BUCKETS = {
    "processed": "processed",
    "follow_gate_pending": "processed",
    "private_reply_blocked": "blocked",
    "instagram_follow_gate_disabled": "blocked",
    "window_expired": "blocked",
    "failed_graph_api": "failed",
    "failed_unexpected": "failed",
    "comment_unavailable": "failed",
}

Platform = Literal["instagram", "facebook"]


def _graph_client_factory(settings: Settings, http_client: httpx.AsyncClient):
    """Replaceable in tests (fake Graph client recording its calls)."""
    return GraphClient(
        access_token=settings.meta_page_access_token,
        http_client=http_client,
        api_version=settings.graph_api_version,
    )


def _monotonic() -> float:
    return time.monotonic()


class _Unauthorized(Exception):
    pass


class _BadCursor(Exception):
    pass


async def require_console_token(request: Request) -> None:
    expected = get_settings().console_internal_token
    if not expected:
        logger.warning("internal_token_unset")
        raise _Unauthorized()
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme != "Bearer" or not token:
        raise _Unauthorized()
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise _Unauthorized()


class CloseBody(BaseModel):
    reason: Literal["window_expired", "ignored", "comment_unavailable"]


class ReplayRecentBody(BaseModel):
    cursor: str | None = None


class KeywordMatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    keywords: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=100)


class ReviewReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str

    @field_validator("text")
    @classmethod
    def _stripped_length(cls, value: str) -> str:
        value = value.strip()
        # len() counts code points, as `[...text].length` does on the console side.
        if not 1 <= len(value) <= REVIEW_TEXT_MAX:
            raise ValueError("text length")
        return value


def is_certain_refusal(error: BaseException) -> bool:
    """A 4xx with a real, non-transient Meta code: nothing was published (ADR 042 §3)."""
    if not isinstance(error, GraphAPIError):
        return False
    status = error.http_status
    code = error.code
    return (
        isinstance(status, int)
        and 400 <= status < 500
        and isinstance(code, int)
        and not isinstance(code, bool)
        and code not in TRANSIENT_META_CODES
    )


def safe_meta_message(error: GraphAPIError, page_token: str) -> str:
    message = error.meta_message if isinstance(error.meta_message, str) else ""
    if page_token:
        message = message.replace(page_token, "[redacted]")
    return message[:META_MESSAGE_MAX]


def _status_or(store: CommentReviewStore, platform: str, comment_id: str, fallback: str) -> str:
    try:
        return store.status_of(platform, comment_id) or fallback
    except Exception:  # noqa: BLE001 - a failed re-read must not turn into a 500
        return fallback


async def review_reply(platform: str, comment_id: str, text: str, settings: Settings) -> JSONResponse:
    store = CommentReviewStore(settings.processed_comments_database)
    if store.status_of(platform, comment_id) is None:
        return JSONResponse(status_code=404, content={"error": "unknown_item"})
    # Opened before the claim: a failure here leaves the item untouched (no claim, no Graph call).
    try:
        OutboundMessageStore(settings.processed_comments_database)
    except Exception as error:  # noqa: BLE001 - nothing claimed yet: a plain retryable error
        logger.warning("review_reply_store_unavailable %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"error": "internal_error"})
    if not store.claim_for_reply(platform, comment_id):
        status = _status_or(store, platform, comment_id, "pending")
        return JSONResponse(status_code=200, content={"result": "not_pending", "status": status})

    recipient_id = facebook_graph_comment_id(comment_id) if platform == "facebook" else comment_id
    try:
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_S) as http_client:
            graph_client = _graph_client_factory(settings, http_client)
            if platform == "facebook":
                result = await graph_client.reply_to_facebook_comment(recipient_id, text)
            else:
                result = await graph_client.reply_to_instagram_comment(recipient_id, text)
    except Exception as error:  # noqa: BLE001 - every outcome after the claim is answered
        if is_certain_refusal(error):
            try:
                store.release_after_error(platform, comment_id)
            except Exception as release_error:  # noqa: BLE001
                logger.warning("review_reply_uncertain %s", type(release_error).__name__)
                status = _status_or(store, platform, comment_id, "sending")
                return JSONResponse(status_code=200, content={"result": "uncertain", "status": status})
            return JSONResponse(
                status_code=200,
                content={
                    "result": "graph_error",
                    "status": _status_or(store, platform, comment_id, "pending"),
                    "code": error.code,
                    "subcode": error.subcode if isinstance(error.subcode, int) else None,
                    "message": safe_meta_message(error, settings.meta_page_access_token),
                },
            )
        logger.warning("review_reply_uncertain %s", type(error).__name__)
        status = _status_or(store, platform, comment_id, "sending")
        return JSONResponse(status_code=200, content={"result": "uncertain", "status": status})

    result = result if isinstance(result, dict) else {}
    posted_reply_id = str(result.get("id") or result.get("message_id") or "")
    try:
        store.complete_reply(
            platform,
            comment_id,
            posted_reply_id=posted_reply_id,
            text=text,
            outbound={"recipient_id": recipient_id, "meta_response_id": posted_reply_id or None},
        )
    except Exception as error:  # noqa: BLE001 - published but not recorded: uncertain
        logger.warning("review_reply_uncertain %s", type(error).__name__)
        status = _status_or(store, platform, comment_id, "sending")
        return JSONResponse(status_code=200, content={"result": "uncertain", "status": status})
    status = _status_or(store, platform, comment_id, "replied")
    return JSONResponse(
        status_code=200, content={"result": "replied", "status": status, "posted_reply_id": posted_reply_id}
    )


def encode_cursor(sweep_started: str, after: tuple[str, str, str] | None) -> str:
    created_at, platform, comment_id = after if after else (None, None, None)
    raw = json.dumps({"s": sweep_started, "c": created_at, "p": platform, "i": comment_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, tuple[str, str, str] | None]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError):
        raise _BadCursor() from None
    if not isinstance(data, dict) or not isinstance(data.get("s"), str) or not data["s"]:
        raise _BadCursor()
    parts = (data.get("c"), data.get("p"), data.get("i"))
    if all(part is None for part in parts):
        return data["s"], None
    if not all(isinstance(part, str) and part for part in parts):
        raise _BadCursor()
    return data["s"], parts  # type: ignore[return-value]


async def replay_one(
    event_key: tuple[str, str],
    *,
    settings: Settings,
    graph_client,
    processor: CommentProcessor,
) -> dict[str, str] | None:
    """Replay one comment. None if the row does not exist. Shared by the unit and batch routes.

    Order (ADR 031 §2-3): pre-check in the database, then one Graph GET, then the processor.
    Nothing reaches Graph unless the row is replayable and younger than 7 days by the database.
    """
    platform, comment_id = event_key
    store = processor.store
    state = store.replay_state(platform, comment_id)
    if state is None:
        return None
    status = str(state["status"])
    if not state["replayable"]:
        return {"result": "duplicate", "status": status}
    if state["window_expired_by_db"]:
        return {"result": "window_expired", "status": status}

    try:
        data = await graph_client.get_comment(comment_id, platform)
    except GraphAPIError as error:
        return {"result": "comment_unavailable" if error.code == 100 else "failed_graph_api", "status": status}
    except httpx.HTTPError:
        return {"result": "failed_unexpected", "status": status}

    data = data if isinstance(data, dict) else {}
    exceeded = replay.graph_comment_age_exceeded(data, platform, replay._utcnow())
    if exceeded is None:
        return {"result": "comment_unavailable", "status": status}
    if exceeded:
        return {"result": "window_expired", "status": status}
    event = replay.build_replay_event(platform, comment_id, data, settings.meta_page_id)
    if event is None:
        return {"result": "comment_unavailable", "status": status}

    [result] = await processor.process_events([event])
    return {"result": result.status, "status": store.comment_status(platform, comment_id) or status}


def create_internal_app() -> FastAPI:
    app = FastAPI(
        title="meta-webhook internal",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(require_console_token)],
    )

    @app.exception_handler(_Unauthorized)
    async def unauthorized(_request: Request, _exc: _Unauthorized) -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    @app.exception_handler(_BadCursor)
    async def bad_cursor(_request: Request, _exc: _BadCursor) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_cursor"})

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("internal_error %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    @app.get("/internal/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/keywords/match")
    async def keywords_match(body: KeywordMatchBody) -> dict[str, bool | int | None]:
        # Same semantics as meta_cron_verify.find_rule: first keyword (given order) that matches.
        index = next((i for i, keyword in enumerate(body.keywords) if contains_keyword(body.text, keyword)), None)
        return {"matched": index is not None, "index": index}

    @app.post("/internal/comments/replay-recent")
    async def replay_recent(body: ReplayRecentBody) -> dict[str, int | str | None]:
        settings = get_settings()
        store = ProcessedCommentStore(settings.processed_comments_database)
        if body.cursor is None:
            sweep_started, after = store.sqlite_now(), None
        else:
            sweep_started, after = decode_cursor(body.cursor)

        rows = store.replayable_batch(sweep_started, after, BATCH_SIZE)
        counts = {"processed": 0, "blocked": 0, "failed": 0, "skipped": 0}
        last = after
        started = _monotonic()
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_S) as http_client:
            graph_client = _graph_client_factory(settings, http_client)
            processor = build_comment_processor(settings, graph_client)
            for row in rows:
                if _monotonic() - started > BATCH_TIME_BUDGET_S:
                    break
                outcome = await replay_one(
                    (row["platform"], row["comment_id"]),
                    settings=settings,
                    graph_client=graph_client,
                    processor=processor,
                )
                result = outcome["result"] if outcome else "unknown_comment"
                counts[BATCH_BUCKETS.get(result, "skipped")] += 1
                last = (row["created_at"], row["platform"], row["comment_id"])

        remaining = store.count_replayable_after(sweep_started, last)
        return {
            **counts,
            "remaining": remaining,
            "cursor": encode_cursor(sweep_started, last) if remaining else None,
        }

    @app.post("/internal/comments/{platform}/{comment_id}/replay")
    async def replay_comment(
        platform: Platform,
        comment_id: str = Path(pattern=COMMENT_ID_PATTERN),
    ) -> JSONResponse:
        settings = get_settings()
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT_S) as http_client:
            graph_client = _graph_client_factory(settings, http_client)
            processor = build_comment_processor(settings, graph_client)
            outcome = await replay_one(
                (platform, comment_id), settings=settings, graph_client=graph_client, processor=processor
            )
        if outcome is None:
            return JSONResponse(status_code=404, content={"error": "unknown_comment"})
        return JSONResponse(status_code=200, content=outcome)

    @app.post("/internal/comments/{platform}/{comment_id}/close")
    async def close_comment(
        body: CloseBody,
        platform: Platform,
        comment_id: str = Path(pattern=COMMENT_ID_PATTERN),
    ) -> JSONResponse:
        settings = get_settings()
        store = ProcessedCommentStore(settings.processed_comments_database)
        if store.replay_state(platform, comment_id) is None:
            return JSONResponse(status_code=404, content={"error": "unknown_comment"})
        if not store.close_manually(platform, comment_id, CLOSE_REASONS[body.reason]):
            return JSONResponse(status_code=409, content={"error": "not_closable"})
        return JSONResponse(status_code=200, content={"result": "closed", "status": "private_reply_blocked"})

    @app.post("/internal/review/{platform}/{comment_id}/reply")
    async def reply_review_item(
        body: ReviewReplyBody,
        platform: Platform,
        comment_id: str = Path(pattern=COMMENT_ID_PATTERN),
    ) -> JSONResponse:
        return await review_reply(platform, comment_id, body.text, get_settings())

    @app.post("/internal/review/{platform}/{comment_id}/skip")
    async def skip_review_item(
        platform: Platform,
        comment_id: str = Path(pattern=COMMENT_ID_PATTERN),
    ) -> JSONResponse:
        store = CommentReviewStore(get_settings().processed_comments_database)
        if store.status_of(platform, comment_id) is None:
            return JSONResponse(status_code=404, content={"error": "unknown_item"})
        if store.skip_pending(platform, comment_id):
            return JSONResponse(status_code=200, content={"result": "skipped", "status": "skipped"})
        status = _status_or(store, platform, comment_id, "pending")
        return JSONResponse(status_code=200, content={"result": "not_pending", "status": status})

    @app.post("/internal/review/{platform}/{comment_id}/release")
    async def release_review_item(
        platform: Platform,
        comment_id: str = Path(pattern=COMMENT_ID_PATTERN),
    ) -> JSONResponse:
        store = CommentReviewStore(get_settings().processed_comments_database)
        if store.status_of(platform, comment_id) is None:
            return JSONResponse(status_code=404, content={"error": "unknown_item"})
        if store.release_stale_sending(platform, comment_id, RELEASE_AFTER_MINUTES):
            return JSONResponse(status_code=200, content={"result": "released", "status": "pending"})
        status = _status_or(store, platform, comment_id, "sending")
        return JSONResponse(status_code=200, content={"result": "not_releasable", "status": status})

    return app


app = create_internal_app()
