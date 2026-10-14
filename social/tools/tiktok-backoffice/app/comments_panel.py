"""s28 (console ADR 062, D3/D4/D6): open the comments panel of a TikTok video by trusted clicks only.

The right panel of a video arrives on « You may like » (not closed), and the comment icon is a toggle: clicking it
while the panel is open closes it. So the state is read first by a passive snapshot (`PANEL_STATE_JS`, panelState()
of app/tiktok_reply_selectors.py), then:

- `comments` => no click;
- `related` => a click on the « Comments » tab;
- `closed` => a click on the comment icon, a pause, a new snapshot, then the « Comments » tab if `related`;
- `unknown` => the « Comments » tab if there is one, else no click.

Never the icon while a « Comments » tab is visible. `panel_targets` is that pure decision (shared with the reply job,
app/tiktok_reply_page.py); `open_comments_panel` is the reader's executor. A click is `click_selector` on the unique
CSS path of the element; on a `CamofoxHTTPError` (422 ambiguous_selector…) or a `ValueError`, the `console-mouse`
plugin clicks at the element's centre when it is available. A timeout / unreachable Camofox goes up (no fallback).
No synthetic event, no JS click, no scrolling script; the click response (it holds the URL) is never read. The
result carries no URL, no handle, no comment text: state, target names, click path, error class or code, count.
"""

from __future__ import annotations

from typing import Any, Callable

from app.camofox_reader import GUARD_EVAL_TIMEOUT_S, CamofoxHTTPError, CamofoxUnreachable
from app.tiktok_reply_selectors import PANEL_STATE_JS

PANEL_STATES = ("comments", "related", "closed", "unknown")
PANEL_OPEN_SETTLE_S = 1.5
CLICK_TIMEOUT_S = 10.0
MOUSE_PROBE_TIMEOUT_S = 5

TARGETS = ("comments_tab", "comment_icon")


def _target(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("selector"), str) or not raw["selector"]:
        return None
    x, y = raw.get("x"), raw.get("y")
    if type(x) not in (int, float) or type(y) not in (int, float):
        return None
    return {"selector": raw["selector"], "x": x, "y": y}


def panel_snapshot(raw: Any) -> dict[str, Any]:
    """Normalise a panelState() result; anything odd (no dict, unknown state, bad target) is dropped to `unknown`/None."""
    if not isinstance(raw, dict):
        raw = {}
    state = raw.get("state")
    count = raw.get("comment_count")
    return {
        "state": state if state in PANEL_STATES else "unknown",
        "comments_tab": _target(raw.get("comments_tab")),
        "comment_icon": _target(raw.get("comment_icon")),
        "comment_count": count if type(count) is int and count >= 0 else None,
    }


def panel_targets(raw: Any, *, after_icon: bool = False) -> list[str]:
    """The elements to click, in order, for one snapshot (D4). `after_icon`: the snapshot taken after the icon click."""
    snap = panel_snapshot(raw)
    state, tab = snap["state"], snap["comments_tab"]
    if after_icon:
        return ["comments_tab"] if state == "related" and tab else []
    if state == "comments":
        return []
    if tab:
        return ["comments_tab"]  # related or unknown: the tab, never the icon (a toggle) while the tab is visible
    if state == "closed" and snap["comment_icon"]:
        return ["comment_icon"]
    return []


def _error_code(exc: Exception) -> str:
    if isinstance(exc, CamofoxHTTPError):
        return f"http_{exc.status}"
    return type(exc).__name__


def _mouse_available(client: Any, memo: dict[str, Any]) -> bool:
    if "mouse" not in memo:
        memo["mouse"] = bool(client.mouse_available(timeout=MOUSE_PROBE_TIMEOUT_S))
    return memo["mouse"]


def _click(
    client: Any, tab_id: str, target: dict[str, Any], memo: dict[str, Any], expired: Callable[[], bool]
) -> tuple[str | None, str | None]:
    """One trusted click: (via, error). A timeout / unreachable Camofox goes up; other errors are only recorded."""
    try:
        client.click_selector(tab_id, target["selector"], timeout=CLICK_TIMEOUT_S)
        return "selector", None
    except CamofoxUnreachable:
        raise
    except (CamofoxHTTPError, ValueError) as exc:
        error = _error_code(exc)
    if expired() or not _mouse_available(client, memo):
        return None, error
    actions = [{"type": "move", "x": float(target["x"]), "y": float(target["y"])}, {"type": "down"}, {"type": "up"}]
    try:
        client.mouse(tab_id, actions, timeout=CLICK_TIMEOUT_S)
        return "mouse", None
    except CamofoxUnreachable:
        raise
    except (CamofoxHTTPError, ValueError) as exc:
        return None, _error_code(exc)


def open_comments_panel(
    client: Any,
    tab_id: str,
    *,
    pause: Callable[[float], None],
    expired: Callable[[], bool] | None = None,
    memo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One activation of the reader: {attempt: {state, clicked, via, error}, comment_count} (D6).

    `expired()` is checked before every evaluation, click and pause; true => hand back at once. `memo` keeps the
    plugin probe for the whole reading (one `mouse_available` at most).
    """
    is_expired = expired if expired is not None else (lambda: False)
    memo = memo if memo is not None else {}
    attempt: dict[str, Any] = {"state": None, "clicked": None, "via": None, "error": None}
    result: dict[str, Any] = {"attempt": attempt, "comment_count": None}
    clicked: list[str] = []

    def snapshot() -> dict[str, Any]:
        return panel_snapshot(client.evaluate(tab_id=tab_id, expression=PANEL_STATE_JS, timeout=GUARD_EVAL_TIMEOUT_S))

    def click_all(snap: dict[str, Any], names: list[str]) -> bool:
        for name in names:
            if is_expired():
                return False
            via, error = _click(client, tab_id, snap[name], memo, is_expired)
            if error is not None:
                attempt["error"] = error
                return False
            clicked.append(name)
            attempt["clicked"] = "+".join(clicked)
            attempt["via"] = via
        return True

    if is_expired():
        return result
    snap = snapshot()
    attempt["state"] = snap["state"]
    result["comment_count"] = snap["comment_count"]
    names = panel_targets(snap)
    if not click_all(snap, names) or names != ["comment_icon"]:
        return result
    if is_expired():
        return result
    pause(PANEL_OPEN_SETTLE_S)
    if is_expired():
        return result
    after = snapshot()
    click_all(after, panel_targets(after, after_icon=True))
    return result
