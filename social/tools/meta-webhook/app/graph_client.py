from typing import Any

import httpx


# Codes meaning Meta will never accept a private reply for this comment
# (permission, user unreachable, already replied, 7-day window expired).
# Anything else (rate limits, transient 1/2, expired token 190…) is retryable.
PRIVATE_REPLY_BLOCKING_CODES = {10, 200, 230, 551, 10900, 10901, 10903}

# Fields read back when a comment is replayed (s09, console ADR 031 §1).
COMMENT_FIELDS_INSTAGRAM = "text,timestamp,username,from,media{id}"
COMMENT_FIELDS_FACEBOOK = "message,created_time,from"

# s16 (console ADR 044): fields read for the console inbox (conversations of the page, then messages).
CONVERSATION_FIELDS = "id,updated_time,participants,messages.limit(1){id,message,from,created_time}"
MESSAGE_FIELDS = "id,message,from,created_time"


class GraphAPIError(RuntimeError):
    """Raised when Meta Graph API returns an error response."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        meta_message: str | None = None,
        http_status: int | None = None,
        meta_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        # s15 (console ADR 042): raw Meta `error.message` (never an HTML fallback) and HTTP status.
        self.meta_message = meta_message
        self.http_status = http_status
        # s16 (console ADR 044): Meta `error.type` (e.g. OAuthException), None when absent.
        self.meta_type = meta_type

    @property
    def blocks_private_reply(self) -> bool:
        return self.code in PRIVATE_REPLY_BLOCKING_CODES


class GraphClient:
    def __init__(
        self,
        access_token: str,
        http_client: httpx.AsyncClient,
        api_version: str = "v21.0",
    ) -> None:
        self.access_token = access_token
        self.http_client = http_client
        self.base_url = f"https://graph.facebook.com/{api_version}"

    async def like_comment(self, comment_id: str) -> dict[str, Any]:
        return await self._post(f"/{comment_id}/likes")

    async def reply_to_instagram_comment(self, comment_id: str, message: str) -> dict[str, Any]:
        return await self._post(f"/{comment_id}/replies", params={"message": message})

    async def reply_to_facebook_comment(self, comment_id: str, message: str) -> dict[str, Any]:
        return await self._post(f"/{comment_id}/comments", params={"message": message})

    async def private_reply_to_instagram_comment(
        self,
        page_id: str,
        comment_id: str,
        text: str,
        quick_replies: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return await self._private_reply_to_comment(page_id, comment_id, text, quick_replies=quick_replies)

    async def private_reply_to_facebook_comment(self, page_id: str, comment_id: str, text: str) -> dict[str, Any]:
        return await self._private_reply_to_comment(page_id, comment_id, text)

    async def send_direct_message(
        self,
        recipient_id: str,
        text: str,
        quick_replies: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"text": text}
        if quick_replies:
            message["quick_replies"] = quick_replies
        return await self._post(
            "/me/messages",
            json={
                "recipient": {"id": recipient_id},
                "message": message,
            },
        )

    async def get_instagram_comment_media(self, comment_id: str) -> dict[str, Any]:
        return await self._get(f"/{comment_id}", params={"fields": "media{id,permalink,caption}"})

    async def get_comment(self, comment_id: str, platform: str) -> dict[str, Any]:
        """Read a comment back (s09 replay): text, date, author and media, per platform."""
        fields = COMMENT_FIELDS_INSTAGRAM if platform == "instagram" else COMMENT_FIELDS_FACEBOOK
        return await self._get(f"/{comment_id}", params={"fields": fields})

    async def list_conversations(
        self,
        page_id: str,
        platform: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> dict[str, Any]:
        """s16: conversations of the page on one platform (`instagram` or `messenger`), most recent first."""
        params = {"platform": platform, "fields": CONVERSATION_FIELDS, "limit": str(limit)}
        if after is not None:
            params["after"] = after
        return await self._get(f"/{page_id}/conversations", params=params)

    async def get_conversation_participants(self, conversation_id: str) -> dict[str, Any]:
        """s16: participants of one conversation."""
        return await self._get(f"/{conversation_id}", params={"fields": "participants"})

    async def list_conversation_messages(
        self,
        conversation_id: str,
        *,
        limit: int,
        after: str | None = None,
    ) -> dict[str, Any]:
        """s16: messages of one conversation, most recent first (`after` goes back in time)."""
        params = {"fields": MESSAGE_FIELDS, "limit": str(limit)}
        if after is not None:
            params["after"] = after
        return await self._get(f"/{conversation_id}/messages", params=params)

    async def _private_reply_to_comment(
        self,
        page_id: str,
        comment_id: str,
        text: str,
        *,
        quick_replies: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"text": text}
        if quick_replies:
            message["quick_replies"] = quick_replies
        return await self._post(
            f"/{page_id}/messages",
            json={
                "recipient": {"comment_id": comment_id},
                "message": message,
            },
        )

    async def can_reply_privately_to_facebook_comment(self, comment_id: str) -> bool:
        data = await self._get(f"/{comment_id}", params={"fields": "can_reply_privately"})
        return bool(data.get("can_reply_privately"))

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        return await self._request("GET", path, params=params)

    async def _post(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request("POST", path, params=params, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_params = {"access_token": self.access_token}
        if params:
            request_params.update(params)

        response = await self.http_client.request(method, f"{self.base_url}{path}", params=request_params, json=json)
        try:
            data = response.json() if response.content else {}
        except ValueError:
            # Meta occasionally answers with an HTML error page.
            data = {}
        if response.is_error:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            message = error.get("message") or response.text[:300]
            code = error.get("code")
            subcode = error.get("error_subcode")
            raise GraphAPIError(
                f"Meta Graph API error {code}" + (f"/{subcode}" if subcode else "") + f": {message}",
                code=code,
                subcode=subcode,
                meta_message=error.get("message"),
                http_status=response.status_code,
                meta_type=error.get("type") if isinstance(error.get("type"), str) else None,
            )
        return data
