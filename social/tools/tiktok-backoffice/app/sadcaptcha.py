"""SadCaptcha HTTP API client, stdlib only (s22, console ADR 053).

Contract of the official client (gbiz123/tiktok-captcha-solver 0.8.9, api.py — rewritten, no licence):
POST <base>/<type>?licenseKey=<key>, JSON body of base64 images, JSON answer validated by
app.captcha_geometry.validate_response.

The key travels in the URL. Hygiene: an error is SadCaptchaError(kind) with the message
"sadcaptcha <kind>" only — never an URL, a key, a body or a detailed status — raised outside the
`except` block so neither __cause__ nor __context__ keeps the original exception. No retry.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app.captcha_geometry import validate_response

DEFAULT_BASE_URL = "https://www.sadcaptcha.com/api/v1"
RESPONSE_MAX_BYTES = 1024 * 1024
ERROR_KINDS = ("http", "timeout", "invalid")


class SadCaptchaError(RuntimeError):
    def __init__(self, kind: str) -> None:
        super().__init__(f"sadcaptcha {kind}")
        self.kind = kind


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (TimeoutError, socket.timeout))


@dataclass(frozen=True)
class SadCaptchaClient:
    # Never printed (repr=False); base_url is only overridable here (tests), never from the environment.
    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 15.0

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 15.0) -> None:
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "timeout", timeout)

    def _post(self, kind: str, body: dict[str, Any]) -> Any:
        url = f"{self.base_url.rstrip('/')}/{kind}?" + urllib.parse.urlencode({"licenseKey": self.api_key})
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        error: str | None = None
        value: Any = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(RESPONSE_MAX_BYTES + 1)
            if len(raw) > RESPONSE_MAX_BYTES:
                error = "invalid"
            else:
                value = validate_response(kind, json.loads(raw.decode("utf-8")))
        except urllib.error.HTTPError as exc:
            exc.close()
            error = "http"
        except (OSError, http.client.HTTPException) as exc:
            error = "timeout" if _is_timeout(exc) else "http"
        except ValueError:
            error = "invalid"
        if error is not None:
            raise SadCaptchaError(error)
        return value

    def rotate(self, outer_b64: str, inner_b64: str) -> int:
        return self._post("rotate", {"outerImageB64": outer_b64, "innerImageB64": inner_b64})

    def puzzle(self, puzzle_b64: str, piece_b64: str) -> float:
        return self._post("puzzle", {"puzzleImageB64": puzzle_b64, "pieceImageB64": piece_b64})

    def shapes(self, image_b64: str) -> tuple[tuple[float, float], tuple[float, float]]:
        return self._post("shapes", {"imageB64": image_b64})

    def icon(self, challenge: str, image_b64: str) -> list[tuple[float, float]]:
        return self._post("icon", {"challenge": challenge, "imageB64": image_b64})
