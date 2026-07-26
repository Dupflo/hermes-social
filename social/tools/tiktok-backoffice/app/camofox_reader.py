from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_CAMOFOX_BASE_URL = "http://camofox:9377"
DEFAULT_CAMOFOX_USER_ID = "hermes_80317d7dba"

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
    if lowered in {"reply", "add comment..."}:
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
    return comments


@dataclass(frozen=True)
class CamofoxClient:
    base_url: str = DEFAULT_CAMOFOX_BASE_URL
    user_id: str = DEFAULT_CAMOFOX_USER_ID
    timeout: int = 30

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def create_tab(self, *, session_key: str, url: str) -> str:
        response = self._request_json("POST", "/tabs", {"userId": self.user_id, "sessionKey": session_key, "url": url})
        tab_id = response.get("tabId")
        if not tab_id:
            raise RuntimeError(f"Camofox did not return tabId: {response!r}")
        return str(tab_id)

    def evaluate(self, *, tab_id: str, expression: str) -> Any:
        response = self._request_json("POST", f"/tabs/{tab_id}/evaluate", {"userId": self.user_id, "expression": expression})
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
    .filter(x => x.r.x > window.innerWidth * 0.55 && x.r.y < 180)
    .sort((a, b) => a.r.x - b.r.x)[0] || candidates[0];
  if (tab) {
    tab.el.scrollIntoView({block: 'center', inline: 'center'});
    tab.el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
    tab.el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    tab.el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
    tab.el.click();
  }
  return {
    clicked: Boolean(tab),
    target: tab ? {text: tab.text, aria: tab.aria, role: tab.role, cls: tab.cls.slice(0, 120), rect: {x: Math.round(tab.r.x), y: Math.round(tab.r.y), w: Math.round(tab.r.width), h: Math.round(tab.r.height)}} : null,
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
    logged_in: text.includes('Messages') && !text.includes('Log in to follow creators'),
    captcha: /captcha|verify|slider|puzzle/i.test(text),
    comments_index: text.indexOf('Comments'),
    related_tab_active: relatedTabActive,
    body_preview: text.slice(0, 1200),
    active_right_panel_preview: activeRightPanelText.slice(0, 1200),
    comment_nodes: comment_nodes.slice(0, 200)
  };
})()
'''


def fetch_comments_from_camofox(
    *,
    video_url: str,
    base_url: str = DEFAULT_CAMOFOX_BASE_URL,
    user_id: str = DEFAULT_CAMOFOX_USER_ID,
    session_key: str = "tiktok-comments-fetch",
    wait_seconds: float = 8.0,
) -> dict[str, Any]:
    client = CamofoxClient(base_url=base_url, user_id=user_id)
    try:
        tab_id = client.create_tab(session_key=session_key, url=video_url)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        activation_errors: list[str] = []
        activation_result: Any = None
        for selector in (':nth-match(:text("Comments"), 1)', ':nth-match(:text("Comments"), 2)', 'text=Comments'):
            try:
                activation_result = client.click(tab_id=tab_id, selector=selector)
                activation_result["selector"] = selector
                break
            except Exception as exc:
                activation_errors.append(f"{selector}: {exc}")
                time.sleep(1.0)
        if activation_result is None:
            activation_result = client.evaluate(tab_id=tab_id, expression=_ACTIVATE_COMMENTS_TAB_JS)
            if isinstance(activation_result, dict):
                activation_result["fallback"] = "js_click"
                activation_result["click_errors"] = activation_errors
        time.sleep(2.0)
        dom_result = client.evaluate(tab_id=tab_id, expression=_COMMENT_EXTRACTION_JS)
    except urllib.error.URLError as exc:
        return {"ok": False, "error": "camofox_unreachable", "detail": str(exc), "comments": []}
    except Exception as exc:
        return {"ok": False, "error": "camofox_fetch_failed", "detail": str(exc), "comments": []}
    if not isinstance(dom_result, dict):
        return {"ok": False, "error": "unexpected_camofox_result", "comments": []}
    comments = extract_comments_from_dom_result(dom_result)
    return {
        "ok": True,
        "video_url": video_url,
        "logged_in": bool(dom_result.get("logged_in")),
        "captcha": bool(dom_result.get("captcha")),
        "comments": comments,
        "diagnostics": {
            "activation": activation_result if isinstance(activation_result, dict) else None,
            "comment_nodes": len(dom_result.get("comment_nodes") or []),
            "comments_index": dom_result.get("comments_index"),
            "related_tab_active": dom_result.get("related_tab_active"),
            "title": dom_result.get("title"),
            "url": dom_result.get("url"),
        },
    }
