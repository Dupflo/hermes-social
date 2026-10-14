"""Browser gestures of the console job `tiktok-reply` (s24, console ADR 055: D4, D9, P8, P9).

Pure functions decide on read-only page snapshots (app/tiktok_reply_selectors.py); TikTokReplyPage performs the
gestures through Camofox only: real keystrokes (`type_text`, `/tabs/:id/type`), `Shift+Enter` between two lines of a
text (a bare Enter would send a half-typed DM), clicks by `click_selector`, or by the s22 `console-mouse` plugin
when a non-send click by selector fails. No clipboard, no shell, no container command, no « J'aime ».

Send clicks (DM "send", reply "Post") are never retried: an error while clicking one raises SendUncertain (the
action may have happened), every other failure before it raises a PageError subclass (nothing was sent).
At most MAX_TABS Camofox tabs: refused before opening; every tab opened here is closed by `close_all()`.
"""

from __future__ import annotations

import time
import unicodedata
from typing import Any, Callable

from app.camofox_reader import CamofoxError
from app.captcha_selectors import PRESENT_JS
from app.comments_panel import panel_snapshot, panel_targets
from app import tiktok_reply_selectors as sel

MAX_TABS = 9
SESSION_KEY = "console-tiktok-reply"
STEP_TIMEOUT_S = 30.0
NEWLINE_KEY = "Shift+Enter"
DM_VERIFY_LAST = 3


class PageError(RuntimeError):
    """Nothing was sent. `code` is a closed run-level code."""

    code = "browser_unavailable"


class TabsFull(PageError):
    pass


class PageUnusable(PageError):
    """`args[0]` is a code of PAGE_UNUSABLE_CODES."""


PAGE_UNUSABLE_CODES = (
    "unexpected_snapshot",
    "no_reply_button",
    "no_reply_editor",
    "no_dm_editor",
    "no_send_button",
    "dm_editor_not_empty",
)


class LoginGate(PageError):
    code = "session_lost"


class DmUnavailable(PageError):
    """The profile shows no message button: the item fails `dm_unavailable` (no action)."""


class SendUncertain(RuntimeError):
    """The send click itself raised: the action may have happened."""


# --- pure decisions (P8) --------------------------------------------------------------------------------------------


def normalize(text: Any) -> str:
    """NFC, every whitespace run (newlines, non-breaking spaces) folded to one space, ends stripped; punctuation kept."""
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split())


def _same_author(a: Any, b: Any) -> bool:
    return bool(a) and bool(b) and str(a).strip().lower() == str(b).strip().lower()


def find_target(snapshot: dict[str, Any], author: str, text: str) -> dict[str, Any] | None:
    """The comment of `author` whose text equals `text` (case folded), else the first one that contains it."""
    comments = [c for c in (snapshot or {}).get("comments") or [] if isinstance(c, dict)]
    wanted = normalize(text).lower()
    mine = [c for c in comments if _same_author(c.get("author"), author)]
    exact = next((c for c in mine if normalize(c.get("text")).lower() == wanted), None)
    if exact is not None:
        return exact
    return next((c for c in mine if wanted and wanted in normalize(c.get("text")).lower()), None)


def _owner_replies(target: dict[str, Any], owner: str) -> list[dict[str, Any]]:
    return [r for r in (target or {}).get("replies") or [] if isinstance(r, dict) and _same_author(r.get("author"), owner)]


def owner_reply_present(target: dict[str, Any], owner: str) -> bool:
    """A reply of the owner under the target, whatever its text (the cron fallback rule: never two replies)."""
    return bool(_owner_replies(target, owner))


def reply_verified(target: dict[str, Any], owner: str, text: str) -> bool:
    wanted = normalize(text)
    return bool(wanted) and any(wanted in normalize(r.get("text")) for r in _owner_replies(target, owner))


def _outgoing(thread: dict[str, Any]) -> list[str]:
    return [normalize(t) for t in (thread or {}).get("outgoing") or [] if isinstance(t, str)]


def dm_present(thread: dict[str, Any], text: str) -> bool:
    wanted = normalize(text)
    return bool(wanted) and any(wanted in bubble for bubble in _outgoing(thread))


def dm_verified(thread: dict[str, Any], text: str) -> bool:
    """Editor present and empty, no send error, and one of the last 3 outgoing bubbles holds the text."""
    thread = thread or {}
    editor = thread.get("editor")
    if not isinstance(editor, dict) or normalize(editor.get("text")) or thread.get("error"):
        return False
    wanted = normalize(text)
    return bool(wanted) and any(wanted in bubble for bubble in _outgoing(thread)[-DM_VERIFY_LAST:])


def text_lines(text: str) -> list[str]:
    return str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")


# --- gestures -------------------------------------------------------------------------------------------------------


class TikTokReplyPage:
    def __init__(
        self,
        client: Any,
        *,
        owner: str,
        session_key: str = SESSION_KEY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.owner = owner
        self.session_key = session_key
        self.sleep = sleep
        self.open_tabs: list[str] = []
        self._mouse: bool | None = None

    # tabs

    def open_tab(self, url: str) -> str:
        if len(self.client.list_tabs(timeout=STEP_TIMEOUT_S)) >= MAX_TABS:
            raise TabsFull("tabs_full")
        tab = self.client.create_blank_tab(self.session_key, timeout=STEP_TIMEOUT_S)
        self.open_tabs.append(tab)
        self.client.navigate(tab, url)
        return tab

    def open_video(self, url: str) -> str:
        tab = self.open_tab(url)
        self.sleep(sel.VIDEO_SETTLE_S)
        return tab

    def close(self, tab: str) -> None:
        if tab in self.open_tabs:
            self.open_tabs.remove(tab)
        try:
            self.client.delete_tab(tab, timeout=STEP_TIMEOUT_S)
        except (CamofoxError, ValueError, OSError):
            pass

    def close_all(self) -> None:
        for tab in list(self.open_tabs):
            self.close(tab)

    # reading

    def _eval(self, tab: str, expression: str) -> Any:
        return self.client.evaluate_value(tab, expression, timeout=STEP_TIMEOUT_S)

    def captcha_present(self, tab: str) -> bool:
        return self._eval(tab, PRESENT_JS) is True

    def _snapshot(self, tab: str, expression: str) -> dict[str, Any]:
        snap = self._eval(tab, expression)
        if not isinstance(snap, dict):
            raise PageUnusable("unexpected_snapshot")
        if snap.get("login_gate"):
            raise LoginGate("login_gate")
        return snap

    def comment_snapshot(self, tab: str, *, author: str, text: str) -> dict[str, Any]:
        expression = sel.comments_js(self.owner)
        snap = self._snapshot(tab, expression)
        if not snap.get("panel_open") and self._open_comments_panel(tab):
            self.sleep(sel.PANEL_SETTLE_S)
            snap = self._snapshot(tab, expression)
        target = find_target(snap, author, text)
        views = [v for v in (target or {}).get("view_replies") or [] if isinstance(v, dict)]
        if views:
            for view in views[: sel.MAX_VIEW_REPLIES_CLICKS]:
                self._click(tab, view)
                self.sleep(sel.REPLIES_SETTLE_S)
            snap = self._snapshot(tab, expression)
        return snap

    def _open_comments_panel(self, tab: str) -> bool:
        """s28 (console ADR 062, D7): panelState() and the shared decision; True when something was clicked.

        « You may like » => the « Comments » tab; closed => the icon, a pause, a new panelState(), then the tab if
        « You may like »; never the icon (a toggle: it would close the panel) while the « Comments » tab is visible.
        """
        state = panel_snapshot(self._eval(tab, sel.PANEL_STATE_JS))
        names = panel_targets(state)
        for name in names:
            self._click(tab, state[name])
        if names == ["comment_icon"]:
            self.sleep(sel.PANEL_SETTLE_S)
            after = panel_snapshot(self._eval(tab, sel.PANEL_STATE_JS))
            for name in panel_targets(after, after_icon=True):
                self._click(tab, after[name])
        return bool(names)

    def thread_snapshot(self, tab: str) -> dict[str, Any]:
        return self._snapshot(tab, sel.THREAD_JS)

    # clicks and typing

    def _mouse_available(self) -> bool:
        if self._mouse is None:
            self._mouse = bool(self.client.mouse_available())
        return self._mouse

    def _click(self, tab: str, element: dict[str, Any]) -> None:
        """Non-send click: by selector, else by the mouse plugin at the element's centre."""
        try:
            self.client.click_selector(tab, element["selector"], timeout=STEP_TIMEOUT_S)
        except (CamofoxError, ValueError):
            if not self._mouse_available():
                raise
            x, y = float(element["x"]), float(element["y"])
            self.client.mouse(tab, [{"type": "move", "x": x, "y": y}, {"type": "down"}, {"type": "up"}])

    def _send_click(self, tab: str, element: dict[str, Any]) -> None:
        """The action itself: one click by selector, never retried; any error = SendUncertain."""
        try:
            self.client.click_selector(tab, element["selector"], timeout=STEP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - the class only travels
            raise SendUncertain(type(exc).__name__) from None

    def _type(self, tab: str, selector: str, text: str) -> None:
        for index, line in enumerate(text_lines(text)):
            if index:
                self.client.press(tab, NEWLINE_KEY, timeout=STEP_TIMEOUT_S)
            if line:
                self.client.type_text(tab, selector, line, timeout=STEP_TIMEOUT_S)

    # reply

    def post_reply(self, tab: str, target: dict[str, Any], text: str) -> None:
        button = (target or {}).get("reply_button")
        if not isinstance(button, dict):
            raise PageUnusable("no_reply_button")
        self._click(tab, button)
        self.sleep(sel.REPLIES_SETTLE_S)
        form = self._snapshot(tab, sel.REPLY_EDITOR_JS)
        editor, post = form.get("editor"), form.get("post_button")
        if not isinstance(editor, dict) or not isinstance(post, dict):
            raise PageUnusable("no_reply_editor")
        self._type(tab, editor["selector"], text)
        self._send_click(tab, post)
        self.sleep(sel.AFTER_SEND_SETTLE_S)

    # DM

    def open_dm(self, handle: str) -> str:
        tab = self.open_tab(sel.profile_url(handle))
        self.sleep(sel.PROFILE_SETTLE_S)
        profile = self._snapshot(tab, sel.PROFILE_JS)
        button = profile.get("message_button")
        if not isinstance(button, dict):
            raise DmUnavailable("dm_unavailable")
        self._click(tab, button)
        self.sleep(sel.THREAD_SETTLE_S)
        return tab

    def type_dm(self, tab: str, text: str) -> None:
        editor = self.thread_snapshot(tab).get("editor")
        if not isinstance(editor, dict):
            raise PageUnusable("no_dm_editor")
        if normalize(editor.get("text")):
            # s25 (s24 m5, console ADR 059, P7): a draft is never completed nor sent; nothing typed
            raise PageUnusable("dm_editor_not_empty")
        self._type(tab, editor["selector"], text)

    def send_dm(self, tab: str) -> None:
        button = self.thread_snapshot(tab).get("send_button")
        if not isinstance(button, dict):
            raise PageUnusable("no_send_button")
        self._send_click(tab, button)
        self.sleep(sel.AFTER_SEND_SETTLE_S)
