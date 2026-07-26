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
        text = _clean_comment_text(node.get("text"))
        if not _looks_like_comment(text):
            continue
        author = _normalize_author(node.get("author"))
        comment_id = node.get("comment_id") or node.get("id")
        comment_id = str(comment_id).strip() if comment_id is not None and str(comment_id).strip() else None
        key = (author, text, comment_id)
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
    const scoped = el.closest('[id], [data-id], [data-comment-id], [data-e2e*="comment"]') || el;
    for (const attr of ['data-comment-id','data-id','id']) {
      const value = scoped.getAttribute?.(attr);
      if (value && value.length > 3 && value.length < 100) return value;
    }
    return null;
  }
  const containerSelector = [
    '[data-e2e*="comment-item"]',
    '[data-e2e*="comment-level"]',
    '[class*="CommentItem"]',
    '[class*="comment-item" i]',
    '[class*="DivComment"]'
  ].join(',');
  const containers = Array.from(document.querySelectorAll(containerSelector))
    .filter(e => !e.closest('#app-header, [data-e2e="inbox-notifications"], [data-e2e="recommend-list-item-container"], [data-e2e="feed-video"]'));
  const comment_nodes = [];
  const seen = new Set();
  for (const container of containers) {
    const candidates = Array.from(container.querySelectorAll('p, span, div')).concat([container]);
    const texts = candidates.map(e => clean(e.innerText || e.textContent))
      .filter(txt => txt && txt.length >= 2 && txt.length <= 500 && !badTexts.has(txt) && !/^\d+[KkMm]?$/.test(txt))
      .filter(txt => !/You may like|We're having trouble playing|© 2026 TikTok/i.test(txt))
      .filter(txt => !['search','upload','profile','notifications','system notifications','following accounts','post video','select language','english'].includes(txt.toLowerCase()))
      .filter(txt => !/^· /.test(txt) && !/ ago$/.test(txt));
    if (!texts.length) continue;
    const author = authorFrom(container);
    const comment_id = commentIdFrom(container);
    for (const txt of texts) {
      if (author && txt.replace(/^@/, '') === author.replace(/^@/, '')) continue;
      const key = `${author || ''}␟${txt}␟${comment_id || ''}`;
      if (seen.has(key)) continue;
      seen.add(key);
      comment_nodes.push({author, text: txt, comment_id});
    }
  }
  return {
    url: location.href,
    title: document.title,
    logged_in: text.includes('Messages') && !text.includes('Log in to follow creators'),
    captcha: /captcha|verify|slider|puzzle/i.test(text),
    comments_index: text.indexOf('Comments'),
    body_preview: text.slice(0, 1200),
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
            "comment_nodes": len(dom_result.get("comment_nodes") or []),
            "comments_index": dom_result.get("comments_index"),
            "title": dom_result.get("title"),
            "url": dom_result.get("url"),
        },
    }
