import re


def contains_keyword(text: str, keyword: str) -> bool:
    """Return True when keyword appears as a standalone word, case-insensitively."""
    normalized_keyword = keyword.strip()
    if not text or not normalized_keyword:
        return False

    pattern = rf"(?<!\w){re.escape(normalized_keyword)}(?!\w)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match is None:
        return False

    negated_pattern = rf"(?<!\w)(sans|pas\s+de)\s+{re.escape(normalized_keyword)}(?!\w)"
    return re.search(negated_pattern, text, flags=re.IGNORECASE) is None
