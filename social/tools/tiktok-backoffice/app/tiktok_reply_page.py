"""Browser gestures of the console job `tiktok-reply` (s24, console ADR 055: D4, D9, P8, P9).

Pure functions decide on read-only page snapshots (app/tiktok_reply_selectors.py); TikTokReplyPage performs the
gestures through Camofox only: real keystrokes (`type_text`, `/tabs/:id/type`), `Shift+Enter` between two lines of a
text (a bare Enter would send a half-typed DM), clicks by `click_selector`, or by the s22 `console-mouse` plugin
when a non-send click by selector fails. No clipboard, no shell, no container command, no « J'aime ».

Send clicks (DM "send", reply "Post") are never retried: an error while clicking one raises SendUncertain (the
action may have happened), every other failure before it raises a PageError subclass (nothing was sent).
At most MAX_TABS Camofox tabs: refused before opening; every tab opened here is closed by `close_all()`.

s30 (console ADR 064): the DM path. The profile « Message » button is the unique one out of the header and
navigation (never the inbox link); after its real click the editor of the conversation is awaited by bounded reads
(captcha gate at each read, time excluded), one more attempt on a recomputed button, else `no_dm_editor`; no button
=> `no_dm_button` (or `dm_unavailable` on a structural refusal); a new tab => `no_dm_editor`. Typing is guarded by
`dm_already_there` (a bubble or the conversation already holds the text), the send is verified by bounded reads
(`verify_dm`). Every step leaves a `dm_probe` record (structure only, closed keys and values: `probe_line`).
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
import urllib.parse
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
    "no_dm_button",
)


class LoginGate(PageError):
    code = "session_lost"


class DmUnavailable(PageError):
    """The profile refuses DMs (s30: a disabled « Message » button, or no button and a structural refusal wording):
    the item fails `dm_unavailable` (no action)."""


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


def conv_count(thread: dict[str, Any], text: str) -> int:
    """s30 (console ADR 064, A2): occurrences of the normalized text in the conversation column (never printed)."""
    wanted = normalize(text)
    conversation = (thread or {}).get("conversation")
    if not wanted or not isinstance(conversation, str):
        return 0
    return normalize(conversation).count(wanted)


def dm_already_there(thread: dict[str, Any], text: str) -> bool:
    """s30 (R1): guard before typing — an outgoing bubble or the conversation already holds the text.

    Fail-safe against the manual replay of a `failed`/`not_verified` item whose DM may have gone: nothing is typed.
    """
    return dm_present(thread, text) or conv_count(thread, text) > 0


def dm_verified(thread: dict[str, Any], text: str) -> bool:
    """Editor present and empty, no send error, and one of the last 3 outgoing bubbles holds the text — or (s30, A2)
    the conversation column holds it, the guard before typing having required 0 occurrence."""
    thread = thread or {}
    editor = thread.get("editor")
    if not isinstance(editor, dict) or normalize(editor.get("text")) or thread.get("error"):
        return False
    wanted = normalize(text)
    if not wanted:
        return False
    return any(wanted in bubble for bubble in _outgoing(thread)[-DM_VERIFY_LAST:]) or conv_count(thread, text) >= 1


# --- s30 (console ADR 064, D1): the `dm_probe` diagnostic ------------------------------------------------------------
# One line per step of the DM path, closed keys in a fixed order, values from closed shapes only: 0|1, integers,
# tag names, data-e2e values, component names of a class, codes. Anything else prints `other`; a key out of the list
# is never printed. Never a handle, a text, an URL nor an id coming from TikTok (`item=` is the id of our queue).

DM_PROBE_KEYS: dict[str, tuple[str, ...]] = {
    "profile": (
        "captcha", "login_gate", "path_is_profile", "candidates", "chosen", "excluded_btns", "tag", "e2e", "role",
        "cls", "in_view", "x", "y", "w", "h", "nav_msg_links", "dm_block", "disabled", "ms",
    ),
    "thread": (
        "attempt", "renavigated", "click", "tabs_before", "tabs_after", "new_tab", "editor", "waited_ms",
        "captcha_ms", "polls", "editors", "ed_tag", "ed_e2e", "ed_role", "ed_cls", "ed1_tag", "ed1_e2e", "ed1_cls",
        "ed2_tag", "ed2_e2e", "ed2_cls", "path_messages", "chat_items", "send_btn", "send_tag", "send_e2e",
        "send_cls", "dm_block",
    ),
    "verify": ("editor", "editor_empty", "error", "outgoing", "bubble_match", "conv_after", "verified", "polls", "ms"),
}
_PROBE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SIX_DIGITS = re.compile(r"\d{6}")


def probe_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(round(value))) if math.isfinite(value) else "other"
    if isinstance(value, str) and _PROBE_TOKEN.fullmatch(value) and not _SIX_DIGITS.search(value):
        return value
    return "other"


def probe_line(item_id: int, step: str, fields: dict[str, Any]) -> str | None:
    keys = DM_PROBE_KEYS.get(step)
    if keys is None:
        return None
    fields = fields if isinstance(fields, dict) else {}
    values = " ".join(f"{key}={probe_value(fields.get(key))}" for key in keys)
    return f"dm_probe item={int(item_id)} step={step} {values}"


def dm_button_decision(profile: dict[str, Any]) -> str:
    """s30 (console ADR 064, D2, A6): `click` | `dm_unavailable` | `no_dm_button` from a PROFILE_JS snapshot.

    A unique button => click, unless disabled (dm_unavailable). No button: a structural refusal wording =>
    dm_unavailable, else no_dm_button (fail-closed, the item is released); an ambiguous button is never refused.
    """
    button = (profile or {}).get("message_button")
    if isinstance(button, dict) and isinstance(button.get("selector"), str):
        return "dm_unavailable" if profile.get("disabled") else "click"
    probe = profile.get("probe") if isinstance(profile.get("probe"), dict) else {}
    if profile.get("dm_block") and probe.get("chosen") != "ambiguous":
        return "dm_unavailable"
    return "no_dm_button"


def _is_inbox_tab(url: Any) -> bool:
    """A tab on tiktok.com whose path starts with /messages (the URL is read in memory, never printed)."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (host == "tiktok.com" or host.endswith(".tiktok.com")) and parts.path.startswith("/messages")


def _shape_fields(prefix: str, shape: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    shape = shape if isinstance(shape, dict) else {}
    return {f"{prefix}{key}": shape.get(key) for key in keys}


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.owner = owner
        self.session_key = session_key
        self.sleep = sleep
        self.clock = clock
        self.open_tabs: list[str] = []
        self._mouse: bool | None = None
        self._dm_probe: list[tuple[str, dict[str, Any]]] = []

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

    def _click(self, tab: str, element: dict[str, Any], *, mouse: bool = True) -> str:
        """Non-send click: by selector, else (when `mouse`) by the mouse plugin at the element's centre.

        Returns `selector` | `mouse` (s30: the `click` key of the `dm_probe` diagnostic)."""
        try:
            self.client.click_selector(tab, element["selector"], timeout=STEP_TIMEOUT_S)
            return "selector"
        except (CamofoxError, ValueError):
            if not mouse or not self._mouse_available():
                raise
            x, y = float(element["x"]), float(element["y"])
            self.client.mouse(tab, [{"type": "move", "x": x, "y": y}, {"type": "down"}, {"type": "up"}])
            return "mouse"

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

    def take_dm_probe(self) -> list[tuple[str, dict[str, Any]]]:
        """s30 (D1): the `dm_probe` records of the last DM path (profile, thread per attempt, verify), then emptied."""
        probe, self._dm_probe = self._dm_probe, []
        return probe

    def _tab_list(self) -> list[dict[str, Any]]:
        return [t for t in self.client.list_tabs(timeout=STEP_TIMEOUT_S) if isinstance(t, dict)]

    def _read_profile(self, tab: str, captcha_gate: Callable[[str], Any], *, began: float,
                      record: bool) -> dict[str, Any]:
        """The captcha gate first (a captcha on the profile is solved before anything is read or clicked), then
        PROFILE_JS; the first read of a DM path is recorded for the diagnostic."""
        met = bool(captcha_gate(tab))
        snap = self._eval(tab, sel.PROFILE_JS)
        if not isinstance(snap, dict):
            raise PageUnusable("unexpected_snapshot")
        if record:
            probe = snap.get("probe") if isinstance(snap.get("probe"), dict) else {}
            fields: dict[str, Any] = {
                "captcha": met, "login_gate": bool(snap.get("login_gate")), "path_is_profile": snap.get("path_is_profile"),
                "candidates": probe.get("candidates"), "chosen": probe.get("chosen"),
                "excluded_btns": probe.get("excluded_btns"),
            }
            fields.update(_shape_fields("", probe.get("btn"), ("tag", "e2e", "role", "cls", "in_view", "x", "y", "w", "h")))
            fields.update({"nav_msg_links": probe.get("nav_msg_links"), "dm_block": snap.get("dm_block"),
                           "disabled": snap.get("disabled"), "ms": int(round((self.clock() - began) * 1000))})
            self._dm_probe.append(("profile", fields))
        if snap.get("login_gate"):
            raise LoginGate("login_gate")
        return snap

    def open_dm(self, handle: str, *, captcha_gate: Callable[[str], Any]) -> str:
        """s30 (console ADR 064, D2, D3, A4-A6): open the conversation with `handle` by real clicks; the tab once the
        editor of the conversation is shown.

        Profile tab, settle, captcha gate, PROFILE_JS; the « Message » button decides (dm_button_decision); a real
        click (mouse fallback only for a button in the viewport), then the editor is read again every
        DM_EDITOR_POLL_S up to DM_EDITOR_WAIT_S, the captcha gate at every read, its time excluded from the wait. No
        editor: one more attempt on a button recomputed by a new PROFILE_JS — on the current page if it is still the
        profile with a unique button, else after one navigation of the same tab back to the profile. Still none =>
        `no_dm_editor`. A new tab after a click => `no_dm_editor` at once (inbox tabs we caused are closed).
        """
        self._dm_probe = []
        began = self.clock()
        url = sel.profile_url(handle)
        tab = self.open_tab(url)
        self.sleep(sel.PROFILE_SETTLE_S)
        profile = self._read_profile(tab, captcha_gate, began=began, record=True)
        decision = dm_button_decision(profile)
        if decision == "dm_unavailable":
            raise DmUnavailable("dm_unavailable")
        if decision != "click":
            raise PageUnusable("no_dm_button")
        for attempt in range(1, sel.DM_OPEN_ATTEMPTS + 1):
            renavigated = False
            if attempt > 1:
                profile = self._read_profile(tab, captcha_gate, began=began, record=False)
                if not (profile.get("path_is_profile") is True and dm_button_decision(profile) == "click"):
                    self.client.navigate(tab, url)
                    renavigated = True
                    self.sleep(sel.PROFILE_SETTLE_S)
                    profile = self._read_profile(tab, captcha_gate, began=began, record=False)
                if dm_button_decision(profile) != "click":
                    raise PageUnusable("no_dm_editor")  # the button existed at the first read
            if self._open_attempt(tab, profile, captcha_gate, attempt=attempt, renavigated=renavigated):
                return tab
        raise PageUnusable("no_dm_editor")

    def _open_attempt(self, tab: str, profile: dict[str, Any], captcha_gate: Callable[[str], Any], *, attempt: int,
                      renavigated: bool) -> bool:
        button = profile["message_button"]
        probe = profile.get("probe") if isinstance(profile.get("probe"), dict) else {}
        in_view = isinstance(probe.get("btn"), dict) and probe["btn"].get("in_view") is True
        before = self._tab_list()
        known = {str(t.get("tabId")) for t in before}
        fields: dict[str, Any] = {"attempt": attempt, "renavigated": renavigated, "tabs_before": len(before)}
        thread: dict[str, Any] | None = None
        new_tab = False
        polls = 0
        paused = 0.0
        started = self.clock()
        try:
            fields["click"] = self._click(tab, button, mouse=in_view)
            started = self.clock()
            while True:
                self.sleep(sel.DM_EDITOR_POLL_S)
                gate_began = self.clock()
                captcha_gate(tab)
                paused += self.clock() - gate_began
                after = self._tab_list()
                fields["tabs_after"] = len(after)
                fresh = [t for t in after if str(t.get("tabId")) not in known]
                if fresh:
                    new_tab = True
                    for extra in fresh:
                        if _is_inbox_tab(extra.get("url")):
                            self.close(str(extra.get("tabId")))
                    break
                thread = self._snapshot(tab, sel.THREAD_JS)
                polls += 1
                if isinstance(thread.get("editor"), dict):
                    break
                if self.clock() - started - paused >= sel.DM_EDITOR_WAIT_S:
                    break
        finally:
            snap = thread or {}
            tprobe = snap.get("probe") if isinstance(snap.get("probe"), dict) else {}
            fields.update({
                "new_tab": new_tab, "editor": isinstance(snap.get("editor"), dict),
                "waited_ms": int(round((self.clock() - started - paused) * 1000)),
                "captcha_ms": int(round(paused * 1000)), "polls": polls, "editors": tprobe.get("editors"),
            })
            fields.update(_shape_fields("ed_", tprobe.get("ed"), ("tag", "e2e", "role", "cls")))
            fields.update(_shape_fields("ed1_", tprobe.get("ed1"), ("tag", "e2e", "cls")))
            fields.update(_shape_fields("ed2_", tprobe.get("ed2"), ("tag", "e2e", "cls")))
            fields.update({"path_messages": tprobe.get("path_messages"), "chat_items": tprobe.get("chat_items"),
                           "send_btn": isinstance(snap.get("send_button"), dict)})
            fields.update(_shape_fields("send_", tprobe.get("send"), ("tag", "e2e", "cls")))
            fields["dm_block"] = tprobe.get("dm_block")
            self._dm_probe.append(("thread", fields))
        if new_tab:
            raise PageUnusable("no_dm_editor")
        return thread is not None and isinstance(thread.get("editor"), dict)

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

    def verify_dm(self, tab: str, text: str) -> bool:
        """s30 (console ADR 064, D4, A3): the thread read again every DM_EDITOR_POLL_S up to DM_VERIFY_WAIT_S until
        dm_verified; never a click nor a keystroke (the send click is never replayed)."""
        started = self.clock()
        polls = 0
        thread: dict[str, Any] = {}
        verified = False
        try:
            while True:
                thread = self.thread_snapshot(tab)
                polls += 1
                if dm_verified(thread, text):
                    verified = True
                    break
                if self.clock() - started >= sel.DM_VERIFY_WAIT_S:
                    break
                self.sleep(sel.DM_EDITOR_POLL_S)
        finally:
            editor = thread.get("editor") if isinstance(thread, dict) else None
            wanted = normalize(text)
            self._dm_probe.append(("verify", {
                "editor": isinstance(editor, dict),
                "editor_empty": isinstance(editor, dict) and not normalize(editor.get("text")),
                "error": bool(thread.get("error")), "outgoing": len(_outgoing(thread)),
                "bubble_match": bool(wanted) and any(wanted in b for b in _outgoing(thread)[-DM_VERIFY_LAST:]),
                "conv_after": conv_count(thread, text), "verified": verified, "polls": polls,
                "ms": int(round((self.clock() - started) * 1000)),
            }))
        return verified
