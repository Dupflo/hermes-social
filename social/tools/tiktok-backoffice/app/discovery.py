from __future__ import annotations

import html
import re
import urllib.request
from pathlib import Path

from app.models import TikTokVideo

_TIKTOK_VIDEO_RE = re.compile(
    r"(?:https?:\\/\\/www\\.tiktok\\.com|https?://www\.tiktok\.com)?[/\\]+(@[A-Za-z0-9._-]+)[/\\]+video[/\\]+(\d+)"
)


def _normalize_profile(profile: str) -> str:
    profile = profile.strip()
    if profile.startswith("https://") or profile.startswith("http://"):
        match = re.search(r"/(@[A-Za-z0-9._-]+)", profile)
        if match:
            return match.group(1)
    if not profile.startswith("@"):
        profile = "@" + profile
    return profile


def _caption_for_match(text: str, start: int, end: int) -> str | None:
    anchor_start = text.rfind("<a", 0, start)
    anchor_end = text.find("</a>", end)
    if anchor_start == -1 or anchor_end == -1 or anchor_end - anchor_start > 5000:
        return None
    anchor = text[anchor_start : anchor_end + 4]
    for pattern in (
        r"alt=[\"']([^\"']+)[\"']",
        r"aria-label=[\"']([^\"']+)[\"']",
        r"title=[\"']([^\"']+)[\"']",
    ):
        match = re.search(pattern, anchor, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return html.unescape(match.group(1)).strip()
    return None


def parse_profile_video_links(document: str, *, profile: str) -> list[TikTokVideo]:
    target_profile = _normalize_profile(profile).lower()
    text = html.unescape(document).replace("\\/", "/")
    seen: set[str] = set()
    videos: list[TikTokVideo] = []
    for match in _TIKTOK_VIDEO_RE.finditer(text):
        author, video_id = match.groups()
        if author.lower() != target_profile:
            continue
        if video_id in seen:
            continue
        seen.add(video_id)
        videos.append(
            TikTokVideo(
                video_url=f"https://www.tiktok.com/{author}/video/{video_id}",
                video_id=video_id,
                author=author,
                caption=_caption_for_match(text, match.start(), match.end()),
            )
        )
    return videos


def fetch_profile_html(profile: str, *, timeout: int = 30) -> str:
    normalized = _normalize_profile(profile)
    url = f"https://www.tiktok.com/{normalized}"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def discover_profile_videos(*, profile: str, html_file: str | Path | None = None) -> list[TikTokVideo]:
    if html_file is not None:
        document = Path(html_file).read_text(encoding="utf-8", errors="replace")
    else:
        document = fetch_profile_html(profile)
    return parse_profile_video_links(document, profile=profile)
