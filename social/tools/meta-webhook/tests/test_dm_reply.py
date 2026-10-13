"""s17 (console ADR 045 §4) — pure 24-hour reply window and DM text bounds.

`tests/fixtures/reply_window_cases.json` is copied byte for byte into the console
(`test/fixtures/reply-window-cases.json`), where `replyWindow` of `core/inbox/reply.ts` is
checked against the same cases. The file's sha256 is pinned identically on both sides.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.dm_reply import (
    INSTAGRAM_TEXT_MAX_BYTES,
    KEY_PATTERN,
    MESSENGER_TEXT_MAX,
    REPLY_WINDOW,
    format_utc_ms,
    parse_graph_time,
    reply_window,
    text_too_long,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "reply_window_cases.json"
FINGERPRINT = "f135bd276fe1d5234392a33540a461f60623cd4080b99ac214720086c10c785f"


def _cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _now(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_fixture_bytes_have_the_pinned_sha256():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FINGERPRINT
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert len(data["cases"]) >= 15


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["name"])
def test_every_fixture_case(case):
    state, boundary = reply_window(case["messages"], _now(case["now"]))
    assert state == case["expected"]["state"]
    assert (format_utc_ms(boundary) if boundary is not None else None) == case["expected"]["boundary"]


def test_window_is_twenty_four_hours():
    assert REPLY_WINDOW.total_seconds() == 86_400


def test_messages_without_from_me_flag_are_not_incoming():
    messages = [{"created_time": "2026-10-13T08:00:00+0000"}, {"from_me": None, "created_time": "2026-10-13T08:00:00+0000"}]
    assert reply_window(messages, _now("2026-10-13T09:00:00.000Z")) == ("unknown", None)


def test_reply_window_ignores_malformed_entries():
    messages = ["junk", None, {"from_me": False}, {"from_me": False, "created_time": 12}]
    assert reply_window(messages, _now("2026-10-13T09:00:00.000Z")) == ("unknown", None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-12T18:02:11+0000", "2026-10-12T18:02:11.000Z"),
        ("2026-10-12T18:02:11+00:00", "2026-10-12T18:02:11.000Z"),
        ("2026-10-12T18:02:11Z", "2026-10-12T18:02:11.000Z"),
        ("2026-10-12T18:02:11.5+0000", "2026-10-12T18:02:11.500Z"),
        ("2026-10-12T18:02:11.123456789+0000", "2026-10-12T18:02:11.123Z"),
        ("2026-10-12T20:02:11-0130", "2026-10-12T21:32:11.000Z"),
    ],
)
def test_parse_graph_time_accepts_the_console_grammar(value, expected):
    assert format_utc_ms(parse_graph_time(value)) == expected


@pytest.mark.parametrize(
    "value",
    [None, 12, "", "not-a-date", "2026-10-12", "2026-10-12 18:02:11+0000", "2026-10-12T18:02:11",
     "2026-13-01T00:00:00+0000", "2026-10-12T18:02:11+000", "2026-10-12T18:02:11.+0000"],
)
def test_parse_graph_time_refuses_anything_else(value):
    assert parse_graph_time(value) is None


def test_parse_graph_time_truncates_to_the_millisecond_like_javascript():
    parsed = parse_graph_time("2026-10-12T18:02:11.123999+0000")
    assert parsed is not None
    assert parsed.microsecond == 123_000
    assert parsed.tzinfo is not None


def test_format_utc_ms_converts_to_utc():
    value = datetime(2026, 10, 13, 11, 6, 0, 1500, tzinfo=timezone.utc)
    assert format_utc_ms(value) == "2026-10-13T11:06:00.001Z"


def test_key_pattern_bounds():
    import re

    pattern = re.compile(KEY_PATTERN)
    assert pattern.fullmatch("a" * 16)
    assert pattern.fullmatch("A-b_9" * 12 + "abcd")
    assert not pattern.fullmatch("a" * 15)
    assert not pattern.fullmatch("a" * 65)
    assert not pattern.fullmatch("a" * 15 + "/")


@pytest.mark.parametrize(
    ("platform", "text", "too_long"),
    [
        ("messenger", "a" * MESSENGER_TEXT_MAX, False),
        ("messenger", "a" * (MESSENGER_TEXT_MAX + 1), True),
        ("messenger", "😀" * MESSENGER_TEXT_MAX, False),
        ("messenger", "  " + "a" * MESSENGER_TEXT_MAX + "  ", False),
        ("instagram", "a" * INSTAGRAM_TEXT_MAX_BYTES, False),
        ("instagram", "a" * (INSTAGRAM_TEXT_MAX_BYTES + 1), True),
        ("instagram", "é" * 500, False),
        ("instagram", "é" * 500 + "a", True),
        ("instagram", "😀" * 250, False),
        ("instagram", "😀" * 250 + "a", True),
        ("instagram", "  " + "a" * INSTAGRAM_TEXT_MAX_BYTES + "\n", False),
    ],
)
def test_text_too_long(platform, text, too_long):
    assert text_too_long(platform, text) is too_long


def test_limits():
    assert MESSENGER_TEXT_MAX == 2000
    assert INSTAGRAM_TEXT_MAX_BYTES == 1000
