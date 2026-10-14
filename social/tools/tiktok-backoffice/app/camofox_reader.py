from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from app.captcha_selectors import PRESENT_JS


DEFAULT_CAMOFOX_BASE_URL = "http://camofox:9377"
DEFAULT_CAMOFOX_USER_ID = "hermes_80317d7dba"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024

_NAVIGATION_TEXT = {
    "tiktok", "search", "for you", "explore", "following", "friends", "live",
    "messages", "activity", "upload", "profile", "more", "comments", "share",
    "you may like", "see translation", "view all", "notifications", "all activity",
    "likes", "mentions and tags", "followers", "this week", "system notifications",
}


def _normalize_author(author: Any) -> str | None:
    if author is None:
        return None
    text = str(author).strip()
    if not text:
        return None
    if text.startswith("@"):
        return text
    if " " in text or "\n" in text:
        return None
    if text.lower() in _NAVIGATION_TEXT:
        return None
    return "@" + text


def _clean_comment_text(text: Any) -> str:
    return " ".join(str(text or "").split()).strip()


def _looks_like_recommendation_node(node: dict[str, Any]) -> bool:
    blob = " ".join(str(node.get(key) or "") for key in ("id", "comment_id", "class", "cls", "e2e", "container_class"))
    return bool(
        blob
        and any(marker in blob.lower() for marker in (
            "recommend-list-item",
            "relatedtab",
            "divinfocontainer",
            "e9pwkrg",
            "one-column-item",
        ))
    )


def _looks_like_comment(text: str) -> bool:
    lowered = text.lower().strip()
    if not lowered or lowered in _NAVIGATION_TEXT:
        return False
    if lowered.startswith("http"):
        return False
    if len(text) > 500:
        return False
    if text.isdigit():
        return False
    if lowered in {"reply", "add comment...", "log in", "sign up"}:
        return False
    if lowered.startswith("view ") and "repl" in lowered:
        return False
    if " ago reply" in lowered or lowered.endswith(" ago"):
        return False
    if lowered.endswith(" comments") and text.split()[0].isdigit():
        return False
    if "you may like" in lowered:
        return False
    if lowered in {"following accounts", "post video", "select language", "english"}:
        return False
    if lowered.startswith("· ") or lowered.endswith(" ago"):
        return False
    if "we're having trouble playing" in lowered:
        return False
    if "© 2026 tiktok" in lowered:
        return False
    return True



_DATE_LINE_RE = re.compile(r"^(?:\d+[smhdw]|\d{1,2}-\d{1,2}|Yesterday|Today)(?:\s+ago)?$", re.IGNORECASE)


def _looks_like_comment_count_line(text: str) -> bool:
    return bool(re.fullmatch(r"\d+[KkMm]?(?:\s+comments?)?", text.strip()))


def _extract_comment_nodes_from_panel_text(panel_text: str) -> list[dict[str, str]]:
    """Fallback parser for TikTok's Comments panel when DOM class names change."""
    raw_lines = [line.strip() for line in str(panel_text or "").splitlines()]
    lines = [line for line in raw_lines if line]
    if not lines:
        return []
    start = None
    for index, line in enumerate(lines):
        if re.fullmatch(r"\d+[KkMm]?\s+comments?", line, flags=re.IGNORECASE):
            start = index + 1
            break
    if start is None:
        return []
    comments: list[dict[str, str]] = []
    index = start
    seen: set[tuple[str, str]] = set()
    while index < len(lines) - 1:
        line = lines[index]
        lowered = line.lower()
        if lowered.startswith("add comment") or lowered.startswith("you may like"):
            break
        if (
            line in _NAVIGATION_TEXT
            or _looks_like_comment_count_line(line)
            or _DATE_LINE_RE.match(line)
            or lowered == "reply"
            or (lowered.startswith("view ") and "repl" in lowered)
        ):
            index += 1
            continue
        author = line
        text_index = index + 1
        text = lines[text_index]
        if not _looks_like_comment(text):
            index += 1
            continue
        cursor = text_index + 1
        seen_reply = False
        while cursor < len(lines):
            current = lines[cursor]
            current_lower = current.lower()
            if current_lower == "reply":
                seen_reply = True
                cursor += 1
                continue
            if current_lower.startswith("view ") and "repl" in current_lower:
                cursor += 1
                break
            if current_lower.startswith("add comment"):
                break
            if seen_reply and not (_looks_like_comment_count_line(current) or _DATE_LINE_RE.match(current)):
                break
            cursor += 1
            if cursor - text_index > 8:
                break
        # Panel-text fallback often only exposes display names (e.g. "Elsa"),
        # not stable @handles. Preserve those as display text instead of
        # inventing @Elsa, which would be misleading.
        normalized_author = author if not author.startswith("@") else (_normalize_author(author) or author)
        key = (normalized_author, text)
        if key not in seen:
            seen.add(key)
            comments.append({"author": normalized_author, "text": text})
        index = max(cursor, index + 2)
    return comments


def extract_comments_from_dom_result(dom_result: dict[str, Any]) -> list[dict[str, str]]:
    comments: list[dict[str, str]] = []
    seen: set[tuple[str | None, str, str | None]] = set()
    for node in dom_result.get("comment_nodes") or []:
        if not isinstance(node, dict):
            continue
        if _looks_like_recommendation_node(node):
            continue
        text = _clean_comment_text(node.get("text"))
        if not _looks_like_comment(text):
            continue
        author = _normalize_author(node.get("author"))
        comment_id = node.get("comment_id") or node.get("id")
        comment_id = str(comment_id).strip() if comment_id is not None and str(comment_id).strip() else None
        key = (author, text, None)
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, str] = {"text": text}
        if comment_id:
            item["id"] = comment_id
        if author:
            item["author"] = author
        comments.append(item)
    if comments:
        return comments
    if dom_result.get("logged_in") is False and re.search(r"\b(Log\s*in|Sign\s*up)\b", str(dom_result.get("body_preview") or ""), re.IGNORECASE):
        return comments
    for key in ("active_right_panel_preview", "right_panel_text", "body_preview"):
        fallback_nodes = _extract_comment_nodes_from_panel_text(str(dom_result.get(key) or ""))
        if fallback_nodes:
            return fallback_nodes
    return comments


class CamofoxError(RuntimeError):
    """Base of the typed Camofox errors (s20). Messages never carry a body, an URL or the key."""


class CamofoxHTTPError(CamofoxError):
    def __init__(self, status: int) -> None:
        super().__init__(f"camofox http status {int(status)}")
        self.status = int(status)


class CamofoxUnreachable(CamofoxError):
    def __init__(self) -> None:
        super().__init__("camofox unreachable")


@dataclass(frozen=True)
class CamofoxClient:
    base_url: str = DEFAULT_CAMOFOX_BASE_URL
    user_id: str = DEFAULT_CAMOFOX_USER_ID
    timeout: int = 30
    # s20 (console ADR 049): Bearer for the requireAuth routes (storage_state). Never printed.
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "CamofoxClient":
        env = os.environ if environ is None else environ
        return cls(
            base_url=(env.get("CAMOFOX_URL") or "").strip() or DEFAULT_CAMOFOX_BASE_URL,
            user_id=(env.get("CAMOFOX_USER_ID") or "").strip() or DEFAULT_CAMOFOX_USER_ID,
            api_key=(env.get("CAMOFOX_API_KEY") or "").strip(),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = self.base_url.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise CamofoxHTTPError(status) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            raise CamofoxUnreachable() from None

    def health(self, *, timeout: float | None = None) -> dict[str, Any]:
        response = self._request_json("GET", "/health", timeout=timeout)
        return response if isinstance(response, dict) else {}

    def list_tabs(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        response = self._request_json("GET", "/tabs", query={"userId": self.user_id}, timeout=timeout)
        tabs = response.get("tabs") if isinstance(response, dict) else None
        if not isinstance(tabs, list):
            raise ValueError("unexpected tabs payload")
        return [
            {"tabId": str(tab.get("tabId")), "url": str(tab.get("url") or "")}
            for tab in tabs
            if isinstance(tab, dict) and tab.get("tabId")
        ]

    def create_blank_tab(self, session_key: str, *, timeout: float | None = None) -> str:
        # No url: Camofox opens about:blank and restores the persisted profile, without navigating.
        response = self._request_json(
            "POST", "/tabs", {"userId": self.user_id, "sessionKey": session_key}, timeout=timeout
        )
        tab_id = response.get("tabId") if isinstance(response, dict) else None
        if not tab_id:
            raise ValueError("camofox did not return tabId")
        return str(tab_id)

    def delete_tab(self, tab_id: str, *, timeout: float | None = None) -> None:
        self._request_json(
            "DELETE",
            f"/tabs/{urllib.parse.quote(str(tab_id), safe='')}",
            query={"userId": self.user_id},
            timeout=timeout,
        )

    def storage_state(self, timeout: float = 60) -> dict[str, Any]:
        path = f"/sessions/{urllib.parse.quote(self.user_id, safe='')}/storage_state"
        response = self._request_json("GET", path, timeout=timeout)
        if not isinstance(response, dict):
            raise ValueError("unexpected storage_state payload")
        return response

    # --- s21 (console ADR 050): login methods. Typed errors only; a message never carries a body, an URL,
    # the typed text or a response repr.

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        timeout: float | None = None,
        accept: str = "image/png",
        max_bytes: int = SCREENSHOT_MAX_BYTES,
    ) -> bytes:
        url = self.base_url.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Content-Type": "application/json", "Accept": accept}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise CamofoxHTTPError(status) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError):
            raise CamofoxUnreachable() from None
        if len(data) > max_bytes:
            raise ValueError("unexpected screenshot payload size")
        return data

    def _tab_path(self, tab_id: str, action: str) -> str:
        return f"/tabs/{urllib.parse.quote(str(tab_id), safe='')}/{action}"

    def navigate(self, tab_id: str, url: str, *, timeout: float = 35) -> None:
        # Camofox waits page.goto(..., {timeout: 30000}) before answering: 35 s client side.
        response = self._request_json(
            "POST", self._tab_path(tab_id, "navigate"), {"userId": self.user_id, "url": url}, timeout=timeout
        )
        if not isinstance(response, dict):
            raise ValueError("unexpected navigate payload")

    def type_text(
        self, tab_id: str, selector: str, text: str, *, delay_ms: int = 60, timeout: float | None = None
    ) -> None:
        # Always real keystrokes (mode keyboard), never submit/pressEnter. A 409 page_changed is CamofoxHTTPError(409).
        response = self._request_json(
            "POST",
            self._tab_path(tab_id, "type"),
            {"userId": self.user_id, "selector": selector, "text": text, "mode": "keyboard", "delay": int(delay_ms)},
            timeout=timeout,
        )
        if not isinstance(response, dict):
            raise ValueError("unexpected type payload")

    def press(self, tab_id: str, key: str, *, timeout: float | None = None) -> None:
        response = self._request_json(
            "POST", self._tab_path(tab_id, "press"), {"userId": self.user_id, "key": key}, timeout=timeout
        )
        if not isinstance(response, dict):
            raise ValueError("unexpected press payload")

    def click_selector(self, tab_id: str, selector: str, *, timeout: float | None = None) -> None:
        response = self._request_json(
            "POST", self._tab_path(tab_id, "click"), {"userId": self.user_id, "selector": selector}, timeout=timeout
        )
        if not isinstance(response, dict) or not response.get("ok"):
            raise ValueError("unexpected click payload")

    def evaluate_value(self, tab_id: str, expression: str, *, timeout: float | None = None) -> Any:
        response = self._request_json(
            "POST", self._tab_path(tab_id, "evaluate"), {"userId": self.user_id, "expression": expression},
            timeout=timeout,
        )
        if not isinstance(response, dict) or not response.get("ok"):
            raise ValueError("unexpected evaluate payload")
        return response.get("result")

    def screenshot(self, tab_id: str, *, timeout: float | None = None) -> bytes:
        data = self._request_bytes(
            "GET", self._tab_path(tab_id, "screenshot"), query={"userId": self.user_id}, timeout=timeout
        )
        if data[:8] != PNG_SIGNATURE:
            raise ValueError("unexpected screenshot payload")
        return data

    # --- s22 (console ADR 053): console-mouse plugin. Trusted Playwright mouse gestures; typed errors only.

    def mouse(self, tab_id: str, actions: list[dict[str, Any]], *, hold: bool = False, timeout: float = 25.0) -> int:
        response = self._request_json(
            "POST",
            self._tab_path(tab_id, "mouse"),
            {"userId": self.user_id, "actions": actions, "hold": bool(hold)},
            timeout=timeout,
        )
        performed = response.get("performed") if isinstance(response, dict) else None
        if not isinstance(response, dict) or response.get("ok") is not True or type(performed) is not int:
            raise ValueError("unexpected mouse payload")
        return performed

    def mouse_available(self, *, timeout: float = 5.0) -> bool:
        """Probe of the console-mouse plugin. 404 (not loaded), 403, unreachable or odd payload ⇒ False."""
        try:
            response = self._request_json("GET", "/console-mouse/health", timeout=timeout)
        except (CamofoxError, ValueError):
            return False
        return isinstance(response, dict) and response.get("ok") is True and response.get("plugin") == "console-mouse"

    def create_tab(self, *, session_key: str, url: str) -> str:
        response = self._request_json("POST", "/tabs", {"userId": self.user_id, "sessionKey": session_key, "url": url})
        tab_id = response.get("tabId")
        if not tab_id:
            raise RuntimeError(f"Camofox did not return tabId: {response!r}")
        return str(tab_id)

    def evaluate(self, *, tab_id: str, expression: str, timeout: float | None = None) -> Any:
        response = self._request_json(
            "POST", f"/tabs/{tab_id}/evaluate", {"userId": self.user_id, "expression": expression}, timeout=timeout
        )
        if not response.get("ok"):
            raise RuntimeError(f"Camofox evaluate failed: {response!r}")
        return response.get("result")

    def click(self, *, tab_id: str, selector: str) -> dict[str, Any]:
        response = self._request_json("POST", f"/tabs/{tab_id}/click", {"userId": self.user_id, "selector": selector})
        if not response.get("ok"):
            raise RuntimeError(f"Camofox click failed: {response!r}")
        return response


_ACTIVATE_COMMENTS_TAB_JS = r'''
(() => {
  function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
  function visible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.x >= 0 && r.y >= 0;
  }
  function rect(el) {
    const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
  }
  function clickElement(el) {
    el.scrollIntoView({block: 'center', inline: 'center'});
    el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, view: window}));
    el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, view: window}));
    el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, view: window}));
    el.click();
  }
  const beforeText = document.body.innerText || '';
  const beforeHasPanel = /\b\d+\s+comments\b/i.test(beforeText) && /Add comment/i.test(beforeText);
  const commentIcons = Array.from(document.querySelectorAll('[data-e2e="comment-icon"]'))
    .filter(visible)
    .map(el => ({el, r: el.getBoundingClientRect(), text: clean(el.innerText || el.textContent), aria: el.getAttribute('aria-label') || ''}))
    .filter(x => /comment/i.test(x.aria) || /^\d+[KkMm]?$/.test(x.text));
  let clicked = null;
  if (!beforeHasPanel && commentIcons.length) {
    const icon = commentIcons
      .filter(x => x.r.x > window.innerWidth * 0.45 && x.r.y > window.innerHeight * 0.35 && x.r.y < window.innerHeight * 0.9)
      .sort((a, b) => Math.abs(a.r.x - window.innerWidth * 0.68) + Math.abs(a.r.y - window.innerHeight * 0.7)
                  - Math.abs(b.r.x - window.innerWidth * 0.68) - Math.abs(b.r.y - window.innerHeight * 0.7))[0]
      || commentIcons[0];
    clickElement(icon.el);
    clicked = {kind: 'comment_icon', text: icon.text, aria: icon.aria, rect: rect(icon.el)};
  }
  const rightPanel = Array.from(document.querySelectorAll('div, aside, section'))
    .map(el => ({el, r: el.getBoundingClientRect(), text: clean(el.innerText || el.textContent)}))
    .filter(x => x.r.x > window.innerWidth * 0.55 && x.r.width > 200 && /Comments/.test(x.text))
    .sort((a, b) => (b.r.width * b.r.height) - (a.r.width * a.r.height))[0]?.el;
  const scope = rightPanel || document;
  const candidates = Array.from(scope.querySelectorAll('button, [role=button], [role=tab], div, span'))
    .filter(visible)
    .map(el => ({el, r: el.getBoundingClientRect(), text: clean(el.innerText || el.textContent), aria: el.getAttribute('aria-label') || '', role: el.getAttribute('role') || '', cls: String(el.className || '')}))
    .filter(x => x.text === 'Comments' || /Comments/.test(x.aria));
  const tab = candidates
    .filter(x => x.r.x > window.innerWidth * 0.55 && x.r.y < 220)
    .sort((a, b) => a.r.x - b.r.x)[0];
  if (tab) {
    clickElement(tab.el);
    clicked = {kind: 'comments_tab', text: tab.text, aria: tab.aria, role: tab.role, cls: tab.cls.slice(0, 120), rect: rect(tab.el), previous: clicked};
  }
  return {
    clicked: Boolean(clicked),
    target: clicked,
    comment_icon_candidates: commentIcons.map(x => ({text: x.text, aria: x.aria, rect: rect(x.el)})).slice(0, 5),
    right_panel_text: clean((rightPanel || document.body).innerText || '').slice(0, 800),
  };
})()
'''



_COMMENT_EXTRACTION_JS = r'''
(() => {
  const text = document.body.innerText || '';
  const badTexts = new Set(['Search','For You','Explore','Following','Friends','LIVE','Messages','Activity','Upload','Profile','More','Comments','Share','You may like','See translation']);
  function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
  function authorFrom(el) {
    const scopes = [el, el.parentElement, el.parentElement?.parentElement, el.closest('[data-e2e*="comment"], article, li')].filter(Boolean);
    for (const scope of scopes) {
      const links = Array.from(scope.querySelectorAll('a[href^="/@"], a[href*="tiktok.com/@"]'));
      for (const a of links) {
        const href = a.getAttribute('href') || '';
        const match = href.match(/\/@([^/?#]+)/);
        if (match) return '@' + decodeURIComponent(match[1]);
        const t = clean(a.innerText || a.textContent);
        if (t && !badTexts.has(t) && !t.includes(' ')) return t.startsWith('@') ? t : '@' + t;
      }
    }
    return null;
  }
  function commentIdFrom(el) {
    const scoped = el.closest('[data-id], [data-comment-id], [data-e2e*="comment"]') || el;
    for (const attr of ['data-comment-id','data-id']) {
      const value = scoped.getAttribute?.(attr);
      if (value && value.length > 3 && value.length < 100) return value;
    }
    return null;
  }
  function inRightPanel(el) {
    const r = el.getBoundingClientRect();
    return r.x > window.innerWidth * 0.55;
  }
  function isRecommendation(el) {
    const blob = [
      el.id || '',
      String(el.className || ''),
      el.getAttribute?.('data-e2e') || '',
      clean(el.innerText || el.textContent),
    ].join(' ');
    return /recommend-list-item|RelatedTab|You may like|DivRelatedTab|DivInfoContainer|e9pwkrg|recommend/i.test(blob)
      || Boolean(el.closest('[data-e2e="recommend-list-item-container"], [data-e2e="feed-video"], article[id^="one-column-item"]'));
  }
  const activeRightPanelText = clean(Array.from(document.querySelectorAll('div, aside, section'))
    .map(el => ({el, r: el.getBoundingClientRect(), text: clean(el.innerText || el.textContent)}))
    .filter(x => x.r.x > window.innerWidth * 0.55 && x.r.width > 200 && x.r.height > 150)
    .sort((a, b) => (b.r.width * b.r.height) - (a.r.width * a.r.height))[0]?.text || '');
  const relatedTabActive = /You may like/.test(activeRightPanelText) && /recommend-list-item|DivRelatedTab|e9pwkrg|recommend/i.test(document.body.innerHTML);
  const containerSelector = [
    '[class*="DivCommentItemWrapper"]',
    '[class*="CommentItem"]',
    '[class*="CommentObject"]',
    '[data-e2e*="comment-item"]',
    '[data-e2e*="comment-level"]'
  ].join(',');
  const containers = relatedTabActive ? [] : Array.from(document.querySelectorAll(containerSelector))
    .filter(e => inRightPanel(e))
    .filter(e => !isRecommendation(e))
    .filter(e => !e.closest('#app-header, [data-e2e="inbox-notifications"]'));
  const comment_nodes = [];
  const seen = new Set();
  function cleanCommentText(container) {
    const content = container.querySelector('[class*="DivCommentContentWrapper"]') || container;
    const usernameNode = content.querySelector('[data-e2e*="comment-username"], [class*="DivUsernameContentWrapper"]');
    const username = clean(usernameNode?.innerText || usernameNode?.textContent || '');
    let txt = clean(content.innerText || content.textContent);
    if (username && txt.startsWith(username)) txt = clean(txt.slice(username.length));
    txt = clean(txt
      .replace(/\s+(?:\d+[smhdw]|\d+-\d+|Yesterday|Today)\s+ago\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i, '')
      .replace(/\s+Reply(?:\s+\d+)?(?:\s+View\s+\d+\s+repl(?:y|ies))?\s*$/i, '')
      .replace(/\s+View\s+\d+\s+repl(?:y|ies)\s*$/i, '')
      .replace(/\s+\d{1,2}-\d{1,2}\s*$/i, '')
    );
    return txt;
  }
  function authorDisplayFrom(container) {
    const usernameNode = container.querySelector('[data-e2e*="comment-username"], [class*="DivUsernameContentWrapper"]');
    return clean(usernameNode?.innerText || usernameNode?.textContent || '');
  }
  function stableHash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return (h >>> 0).toString(16).padStart(8, '0');
  }
  for (const container of containers) {
    const txt = cleanCommentText(container);
    if (!txt || txt.length < 2 || txt.length > 500 || badTexts.has(txt) || /^\d+[KkMm]?$/.test(txt)) continue;
    if (/You may like|We're having trouble playing|© 2026 TikTok|^Reply$|^View \d+ repl/i.test(txt)) continue;
    const author = authorFrom(container);
    const display = authorDisplayFrom(container);
    if (display && txt === display) continue;
    const stableKey = `${location.pathname}:${author || display || 'unknown'}:${txt}`;
    const comment_id = commentIdFrom(container) || `${author || display || 'unknown'}:${stableHash(stableKey)}`;
    const key = `${author || display || ''}␟${txt}␟${comment_id || ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    comment_nodes.push({author, display_author: display, text: txt, comment_id, container_class: String(container.className || '').slice(0, 160)});
  }
  return {
    url: location.href,
    title: document.title,
    logged_in: text.includes('Messages') && !/\b(Log\s*in(?:\s+to\s+comment)?|Sign\s*up)\b/i.test(text),
    captcha: /captcha|verify|slider|puzzle/i.test(text),
    comments_index: text.indexOf('Comments'),
    related_tab_active: relatedTabActive,
    body_preview: text.slice(0, 5000),
    active_right_panel_preview: activeRightPanelText.slice(0, 5000),
    comment_nodes: comment_nodes.slice(0, 200)
  };
})()
'''



def _expected_comment_count_from_activation(activation_result: Any) -> int | None:
    if not isinstance(activation_result, dict):
        return None
    candidates = activation_result.get("comment_icon_candidates") or []
    for candidate in candidates:
        blob = " ".join(str(candidate.get(key) or "") for key in ("text", "aria"))
        match = re.search(r"\b(\d+(?:[.,]\d+)?)([KkMm]?)\s+comments?\b", blob) or re.search(r"^\s*(\d+(?:[.,]\d+)?)([KkMm]?)\s*$", blob)
        if not match:
            continue
        value = float(match.group(1).replace(",", "."))
        suffix = match.group(2).lower()
        if suffix == "k":
            value *= 1000
        elif suffix == "m":
            value *= 1000000
        return int(value)
    return None


def _right_panel_looks_like_recommendations(dom_result: dict[str, Any]) -> bool:
    panel = str(dom_result.get("active_right_panel_preview") or dom_result.get("right_panel_text") or "")
    if "You may like" not in panel:
        return False
    if re.search(r"\b\d+[KkMm]?\s+comments?\b", panel, flags=re.IGNORECASE):
        return False
    return bool(re.search(r"#|\b\d+[smhdw]\s+ago\b|\b\d{1,2}-\d{1,2}\b|fyp|foryou", panel, flags=re.IGNORECASE))



def _video_author_from_url(video_url: str) -> str | None:
    match = re.search(r"tiktok\.com/@([^/?#]+)/video/", video_url)
    return "@" + match.group(1) if match else None


def _video_id_from_url(video_url: str) -> str | None:
    match = re.search(r"/video/(\d+)", video_url)
    return match.group(1) if match else None


def _target_video_coherence(video_url: str, dom_result: dict[str, Any], comments: list[dict[str, str]]) -> dict[str, Any]:
    """Detect TikTok SPA/feed incoherence before ingesting or acting.

    TikTok can leave location.href/title on the requested video while the visible
    body/right panel belong to a recommended feed video. URL/title alone are not
    enough; require the target creator handle to be present in the body/panel or
    at least in extracted comment authors before trusting the result.
    """
    target_author = _video_author_from_url(video_url)
    target_video_id = _video_id_from_url(video_url)
    loaded_url = str(dom_result.get("url") or "")
    text_blob = "\n".join(
        str(dom_result.get(key) or "")
        for key in ("body_preview", "active_right_panel_preview", "title")
    )
    lowered = text_blob.lower()
    author_token = (target_author or "").lower().lstrip("@")
    loaded_has_video_id = bool(target_video_id and target_video_id in loaded_url)
    loaded_is_other_video = bool(target_video_id and "/video/" in loaded_url and not loaded_has_video_id)
    body_has_author = bool(author_token and author_token in lowered)
    comments_have_author = bool(
        author_token
        and any(author_token in str(item.get("author") or "").lower().lstrip("@") for item in comments)
    )
    logged_in = bool(dom_result.get("logged_in"))
    login_gate = (dom_result.get("logged_in") is False) and bool(re.search(r"\b(Log\s*in|Sign\s*up)\b", str(dom_result.get("body_preview") or ""), re.IGNORECASE))

    # Logged-out gates are handled separately: they are not a cross-video hazard,
    # but they are still non-actionable. A different loaded /video/ URL is always
    # incoherent; TikTok may silently jump to a recommended video while preserving
    # the target creator in right-rail text, which previously caused false trust.
    coherent = True
    reason = None
    if loaded_is_other_video:
        coherent = False
        reason = "loaded_different_video"
    elif target_author and loaded_has_video_id and logged_in and not (body_has_author or comments_have_author):
        coherent = False
        reason = "target_author_missing_from_loaded_body"
    return {
        "coherent": coherent,
        "reason": reason,
        "target_author": target_author,
        "target_video_id": target_video_id,
        "loaded_url": loaded_url,
        "loaded_has_video_id": loaded_has_video_id,
        "body_has_author": body_has_author,
        "comments_have_author": comments_have_author,
        "logged_in": logged_in,
        "login_gate": login_gate,
    }


def _reload_target_video_expression(video_url: str, attempt: int) -> str:
    url = json.dumps(video_url)
    return f"location.replace({url} + ({url}.includes('?') ? '&' : '?') + '_hermes_reload={attempt}_' + Date.now()); true"


def fetch_comments_from_camofox(
    *,
    video_url: str,
    base_url: str | None = None,
    user_id: str | None = None,
    session_key: str = "tiktok-comments-fetch",
    wait_seconds: float = 8.0,
    client: CamofoxClient | None = None,
    on_captcha: Callable[[str], bool] | None = None,
    expired: Callable[[], bool] | None = None,
    close_tab: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Read the visible comments of one video.

    s23 (console ADR 054, P3): the keyword options are additive. With their defaults the behaviour is the CLI /
    legacy cron one (no captcha probe, tab left open, text-regex captcha flag). With `on_captcha`, the captcha
    selectors (`captcha_selectors.PRESENT_JS`) are probed at the start of every attempt and before every reload; a
    captcha goes to `on_captcha(tab_id)` in the current tab (True => go on without that reload, False =>
    `captcha_unsolved`). `expired()` true => `video_timeout`. `close_tab` deletes the tab at the end, errors
    swallowed. `client` given => `base_url`/`user_id` ignored.
    """
    if client is None:
        # s20: defaults come from the environment (CAMOFOX_URL, CAMOFOX_USER_ID, CAMOFOX_API_KEY);
        # explicit values keep priority.
        client = CamofoxClient.from_env()
        if base_url:
            client = replace(client, base_url=base_url)
        if user_id:
            client = replace(client, user_id=user_id)
    tab: dict[str, str | None] = {"id": None}
    try:
        return _fetch_comments_in_tab(
            client,
            tab,
            video_url=video_url,
            session_key=session_key,
            wait_seconds=wait_seconds,
            on_captcha=on_captcha,
            expired=expired,
            # Resolved at call time, so monkeypatching camofox_reader.time.sleep keeps working.
            pause=sleep if sleep is not None else time.sleep,
        )
    finally:
        if close_tab and tab["id"] is not None:
            try:
                client.delete_tab(tab["id"], timeout=10)
            except Exception:  # noqa: BLE001 - closing is best effort, the result stands
                pass


def _fetch_comments_in_tab(
    client: Any,
    tab: dict[str, str | None],
    *,
    video_url: str,
    session_key: str,
    wait_seconds: float,
    on_captcha: Callable[[str], bool] | None,
    expired: Callable[[], bool] | None,
    pause: Callable[[float], None],
) -> dict[str, Any]:
    activation_result: Any = None
    activation_errors: list[str] = []
    dom_result: dict[str, Any] | None = None
    comments: list[dict[str, str]] = []
    coherence: dict[str, Any] | None = None
    captcha_present = False  # last PRESENT_JS probe (scan mode only)

    def captcha_in_the_way(tab_id: str) -> bool | None:
        """Scan mode: probe the selectors; None = no captcha, True = solved, False = unsolved."""
        nonlocal captcha_present
        captcha_present = client.evaluate(tab_id=tab_id, expression=PRESENT_JS) is True
        if not captcha_present:
            return None
        solved = bool(on_captcha(tab_id))
        captcha_present = not solved
        return solved

    try:
        tab_id = client.create_tab(session_key=session_key, url=video_url)
        tab["id"] = tab_id
        if wait_seconds > 0:
            pause(wait_seconds)
        for attempt in range(3):
            if expired is not None and expired():
                return {"ok": False, "error": "video_timeout", "comments": []}
            if on_captcha is not None and captcha_in_the_way(tab_id) is False:
                return {"ok": False, "error": "captcha_unsolved", "comments": []}
            activation_errors = []
            # Generic text selectors are unsafe on TikTok: they can select the
            # adjacent/right-rail recommendation tab ("You may like") while still
            # reporting a successful "Comments" click. Use the DOM-scoped activator
            # only: comment icon first, then the visible Comments tab in the right panel.
            activation_result = client.evaluate(tab_id=tab_id, expression=_ACTIVATE_COMMENTS_TAB_JS)
            if isinstance(activation_result, dict):
                activation_result["fallback"] = "js_click_scoped_comments_panel"
                activation_result["click_errors"] = activation_errors
            pause(2.0)
            raw_dom_result = client.evaluate(tab_id=tab_id, expression=_COMMENT_EXTRACTION_JS)
            if not isinstance(raw_dom_result, dict):
                return {"ok": False, "error": "unexpected_camofox_result", "comments": []}
            dom_result = raw_dom_result
            comments = extract_comments_from_dom_result(dom_result)
            coherence = _target_video_coherence(video_url, dom_result, comments)
            if coherence.get("coherent"):
                break
            if attempt < 2:
                if expired is not None and expired():
                    return {"ok": False, "error": "video_timeout", "comments": []}
                if on_captcha is not None:
                    solved = captcha_in_the_way(tab_id)
                    if solved is False:
                        return {"ok": False, "error": "captcha_unsolved", "comments": []}
                    if solved is True:
                        continue  # never reload a page that just had a captcha: go on in the same tab
                client.evaluate(tab_id=tab_id, expression=_reload_target_video_expression(video_url, attempt + 1))
                pause(max(wait_seconds, 6.0))
    except (urllib.error.URLError, CamofoxUnreachable, CamofoxHTTPError) as exc:
        return {"ok": False, "error": "camofox_unreachable", "detail": str(exc), "comments": []}
    except Exception as exc:
        return {"ok": False, "error": "camofox_fetch_failed", "detail": str(exc), "comments": []}
    if dom_result is None:
        return {"ok": False, "error": "unexpected_camofox_result", "comments": []}
    # Scan mode: the flag comes from the last selector probe, never from the text regex (a comment saying
    # "verify" is not a captcha).
    captcha_flag = captcha_present if on_captcha is not None else bool(dom_result.get("captcha"))
    expected_comment_count = _expected_comment_count_from_activation(activation_result)
    right_panel_recommendations = _right_panel_looks_like_recommendations(dom_result)
    diagnostics = {
        "activation": activation_result if isinstance(activation_result, dict) else None,
        "comment_nodes": len(dom_result.get("comment_nodes") or []),
        "comments_index": dom_result.get("comments_index"),
        "related_tab_active": dom_result.get("related_tab_active"),
        "expected_comment_count": expected_comment_count,
        "right_panel_recommendations": right_panel_recommendations,
        "title": dom_result.get("title"),
        "url": dom_result.get("url"),
        "target_video_coherence": coherence,
    }
    if coherence and not coherence.get("coherent"):
        return {
            "ok": False,
            "error": "target_video_incoherent",
            "detail": coherence.get("reason"),
            "video_url": video_url,
            "logged_in": bool(dom_result.get("logged_in")),
            "captcha": captcha_flag,
            "comments": [],
            "diagnostics": diagnostics,
        }
    if captcha_flag:
        return {
            "ok": False,
            "error": "captcha_required",
            "detail": "TikTok verification/captcha is visible",
            "video_url": video_url,
            "logged_in": bool(dom_result.get("logged_in")),
            "captcha": True,
            "comments": [],
            "diagnostics": diagnostics,
        }
    if dom_result.get("logged_in") is False and not comments:
        return {
            "ok": False,
            "error": "tiktok_page_not_actionable",
            "detail": "TikTok did not expose a logged-in/comment-readable page",
            "video_url": video_url,
            "logged_in": False,
            "captcha": captcha_flag,
            "comments": [],
            "diagnostics": diagnostics,
        }
    if dom_result.get("related_tab_active") and not comments:
        return {
            "ok": False,
            "error": "comments_tab_not_active",
            "detail": "TikTok related/recommendation tab is active instead of Comments",
            "video_url": video_url,
            "logged_in": bool(dom_result.get("logged_in")),
            "captcha": captcha_flag,
            "comments": [],
            "diagnostics": diagnostics,
        }
    if right_panel_recommendations and not comments:
        return {
            "ok": False,
            "error": "right_panel_recommendations",
            "detail": "TikTok right panel shows recommendations instead of comment rows",
            "video_url": video_url,
            "logged_in": bool(dom_result.get("logged_in")),
            "captcha": captcha_flag,
            "comments": [],
            "diagnostics": diagnostics,
        }
    if expected_comment_count and expected_comment_count > 0 and not comments:
        return {
            "ok": False,
            "error": "comments_expected_but_not_extracted",
            "detail": f"TikTok reports {expected_comment_count} comments but DOM/API extraction returned zero",
            "video_url": video_url,
            "logged_in": bool(dom_result.get("logged_in")),
            "captcha": captcha_flag,
            "comments": [],
            "diagnostics": diagnostics,
        }
    return {
        "ok": True,
        "video_url": video_url,
        "logged_in": bool(dom_result.get("logged_in")),
        "captcha": captcha_flag,
        "comments": comments,
        "diagnostics": diagnostics,
    }
