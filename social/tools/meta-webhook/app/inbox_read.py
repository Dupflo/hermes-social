"""s16 (console ADR 044) — pure normalisation of Graph conversations for the console inbox.

No I/O, no Graph client, no token: `app/internal.py` reads Graph and hands the raw payloads here.
Every field sent to the console is coerced and bounded; a malformed Graph entry is ignored (or its
field nulled), never an exception. Never the participants' `email`, never a URL.

Graph conversation ids and `after` cursors leave Python only as opaque refs (base64url without
padding). A decoded conversation ref is validated before it becomes a Graph path, so a forged ref
(`me/accounts`, `x?y`, `../`) never reaches another Graph node with the page token.
"""

import base64
import binascii
import re
from typing import Any

from app.platform_utils import is_owner_identity


REF_PATTERN = r"^[A-Za-z0-9_-]{1,2048}$"
CONVERSATION_ID_PATTERN = r"^[A-Za-z0-9_:.-]{1,200}$"
TEXT_MAX = 5000
NAME_MAX = 200
ID_MAX = 200
DATE_MAX = 64

_REF_RE = re.compile(REF_PATTERN)
_CONVERSATION_ID_RE = re.compile(CONVERSATION_ID_PATTERN)


def encode_ref(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def decode_ref(ref: str) -> str:
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        raise ValueError("invalid ref")
    try:
        padded = ref + "=" * (-len(ref) % 4)
        value = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeError):
        raise ValueError("invalid ref") from None
    if not value:
        raise ValueError("invalid ref")
    return value


def is_conversation_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_CONVERSATION_ID_RE.fullmatch(value)) and ".." not in value


def decode_conversation_ref(ref: str) -> str:
    value = decode_ref(ref)
    if not is_conversation_id(value):
        raise ValueError("invalid conversation ref")
    return value


def as_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def bounded_str(value: Any, limit: int) -> str | None:
    if isinstance(value, str) and value:
        return value[:limit]
    return None


def _strict_str(value: Any, limit: int) -> str | None:
    """A non-empty string no longer than `limit`, else None (ids and dates are never truncated)."""
    if isinstance(value, str) and value and len(value) <= limit:
        return value
    return None


def _entries(value: Any) -> list[Any]:
    if isinstance(value, dict):
        value = value.get("data")
    return value if isinstance(value, list) else []


def _is_owner(entry: dict[str, Any], owner_ids: set[str], owner_usernames: set[str]) -> bool:
    author_id = entry.get("id")
    username = entry.get("username")
    return is_owner_identity(
        author_id=author_id if isinstance(author_id, str) else None,
        username=username if isinstance(username, str) else None,
        owner_ids=owner_ids,
        owner_usernames=owner_usernames,
    )


def normalize_peer(participants: Any, owner_ids: set[str], owner_usernames: set[str]) -> dict[str, str | None]:
    for entry in _entries(participants):
        if not isinstance(entry, dict) or _is_owner(entry, owner_ids, owner_usernames):
            continue
        return {
            "id": _strict_str(entry.get("id"), ID_MAX),
            "name": bounded_str(entry.get("name"), NAME_MAX),
            "username": bounded_str(entry.get("username"), NAME_MAX),
        }
    return {"id": None, "name": None, "username": None}


def normalize_message(raw: Any, owner_ids: set[str], owner_usernames: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    message_id = _strict_str(raw.get("id"), ID_MAX)
    created_time = _strict_str(raw.get("created_time"), DATE_MAX)
    if message_id is None or created_time is None:
        return None
    sender = raw.get("from")
    from_me = isinstance(sender, dict) and _is_owner(sender, owner_ids, owner_usernames)
    return {
        "id": message_id,
        "text": bounded_str(raw.get("message"), TEXT_MAX),
        "from_me": from_me,
        "created_time": created_time,
    }


def normalize_conversation(raw: Any, owner_ids: set[str], owner_usernames: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not is_conversation_id(raw.get("id")):
        return None
    updated_time = _strict_str(raw.get("updated_time"), DATE_MAX)
    if updated_time is None:
        return None
    last = None
    entries = _entries(raw.get("messages"))
    if entries:
        first = normalize_message(entries[0], owner_ids, owner_usernames)
        if first is not None:
            last = {"text": first["text"], "from_me": first["from_me"], "created_time": first["created_time"]}
    return {
        "ref": encode_ref(raw["id"]),
        "updated_time": updated_time,
        "peer": normalize_peer(raw.get("participants"), owner_ids, owner_usernames),
        "last_message": last,
    }
