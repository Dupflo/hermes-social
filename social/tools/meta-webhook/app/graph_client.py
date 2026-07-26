from typing import Any

import httpx


class GraphAPIError(RuntimeError):
    """Raised when Meta Graph API returns an error response."""


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
    ) -> dict[str, Any]:
        return await self._private_reply_to_comment(page_id, comment_id, text)

    async def private_reply_to_facebook_comment(self, page_id: str, comment_id: str, text: str) -> dict[str, Any]:
        return await self._private_reply_to_comment(page_id, comment_id, text)

    async def send_direct_message(self, recipient_id: str, text: str) -> dict[str, Any]:
        return await self._post(
            "/me/messages",
            json={
                "recipient": {"id": recipient_id},
                "message": {"text": text},
            },
        )

    async def _private_reply_to_comment(self, page_id: str, comment_id: str, text: str) -> dict[str, Any]:
        return await self._post(
            f"/{page_id}/messages",
            json={
                "recipient": {"comment_id": comment_id},
                "message": {"text": text},
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
        data = response.json() if response.content else {}
        if response.is_error:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            message = error.get("message") or response.text
            code = error.get("code")
            raise GraphAPIError(f"Meta Graph API error {code}: {message}")
        return data
