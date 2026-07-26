from __future__ import annotations

import re


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def contains_keyword(text: str, keyword: str) -> bool:
    needle = normalize_text(keyword)
    if not needle:
        return False
    haystack = normalize_text(text)
    return bool(re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack))
