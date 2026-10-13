"""s17 (console ADR 045 §4) — pure rules of a console DM reply: 24-hour window, text bounds, key.

No I/O. The same window rule lives in the console (`core/inbox/reply.ts`, display only), held
identical by the shared fixture `tests/fixtures/reply_window_cases.json`; this one is the guard
applied just before the Graph POST, and it wins in case of divergence.

Dates follow the console's `parseGraphTime` grammar exactly (optional fraction of a second, `Z`,
`+00:00`, `+0000`) and are truncated to the millisecond, as JavaScript does, so the strict
boundary `now < last_incoming + 24 h` is decided identically on both sides.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

REPLY_WINDOW = timedelta(hours=24)
KEY_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"
MESSENGER_TEXT_MAX = 2000
INSTAGRAM_TEXT_MAX_BYTES = 1000

_GRAPH_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})$"
)

WindowState = Literal["open", "closed", "unknown"]


def parse_graph_time(value: Any) -> datetime | None:
    """`2026-10-12T18:02:11+0000` (and the other console forms) as an aware datetime, else None."""
    if not isinstance(value, str):
        return None
    match = _GRAPH_TIME.fullmatch(value)
    if match is None:
        return None
    base, fraction, offset = match.groups()
    if offset == "Z":
        offset = "+00:00"
    elif ":" not in offset:
        offset = f"{offset[:3]}:{offset[3:]}"
    millis = (fraction or "")[:3].ljust(3, "0")
    try:
        return datetime.fromisoformat(f"{base}.{millis}000{offset}")
    except ValueError:
        return None


def format_utc_ms(value: datetime) -> str:
    """ISO UTC with milliseconds, as JavaScript's `toISOString()`."""
    utc = value.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def reply_window(messages: list[Any], now: datetime) -> tuple[WindowState, datetime | None]:
    """Open iff `now < L + 24 h`, L = most recent readable message of the other person.

    `("open", closes_at)`, `("closed", closes_at)`, or `("unknown", None)` when no message of the
    other person is readable among those given (treated as closed by the route).
    """
    latest: datetime | None = None
    for message in messages:
        if not isinstance(message, dict) or message.get("from_me") is not False:
            continue
        created = parse_graph_time(message.get("created_time"))
        if created is not None and (latest is None or created > latest):
            latest = created
    if latest is None:
        return "unknown", None
    closes_at = latest + REPLY_WINDOW
    return ("open" if now < closes_at else "closed"), closes_at


def text_too_long(platform: str, text: str) -> bool:
    """Messenger: 2 000 code points; Instagram: 1 000 UTF-8 bytes; both after strip, never truncated."""
    value = text.strip()
    if platform == "instagram":
        return len(value.encode("utf-8")) > INSTAGRAM_TEXT_MAX_BYTES
    return len(value) > MESSENGER_TEXT_MAX
