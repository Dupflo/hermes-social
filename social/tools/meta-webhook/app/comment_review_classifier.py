import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class CommentReviewDecision:
    should_store: bool
    reason: str
    score: int


COMPLAINT_RE = re.compile(
    r"\b(rien re[cç]u|pas re[cç]u|que dalle|pas de dm|pas de mp|marche pas|fonctionne pas|n['’]?envoi(?:e|s)? rien|envoi(?:e|s)? que dalle)\b",
    re.IGNORECASE,
)
BUSINESS_RE = re.compile(
    r"\b(travailler avec toi|message priv[eé]|collab|collaboration|projet client|beta testeur|b[eê]ta testeur|partenariat)\b",
    re.IGNORECASE,
)
SKEPTICAL_OR_SARCASTIC_RE = re.compile(
    r"\b(sceptique|hautain|pr[eé]tentieux|arnaque|fake|bidon|r[eé]seau|gagner\s+\d+\s?e|haha|mdr)\b",
    re.IGNORECASE,
)
REQUEST_RE = re.compile(
    r"\b(lien|link|ressource|resource|outil|tool|nom|site|guide|comment faire|tu peux|peux[- ]?tu|stp|svp|int[eé]ress[ée]?|dm|mp|envoie|envoi)\b",
    re.IGNORECASE,
)
NOISE = {
    "merci",
    "merci beaucoup",
    "top",
    "bravo",
    "cool",
    "super",
    "excellent",
    "incroyable",
    "oui",
    "non",
    "hello",
    "bonjour",
    "salut",
    "tres clair merci",
    "très clair merci",
}


def classify_comment_for_review(
    *,
    text: str,
    has_owner_reply: bool,
    matches_active_campaign: bool,
    already_terminal: bool,
) -> CommentReviewDecision:
    normalized = _normalize(text)
    if already_terminal:
        return CommentReviewDecision(False, "already_terminal", 0)

    if COMPLAINT_RE.search(text):
        return CommentReviewDecision(True, "automation_complaint", 100)

    if has_owner_reply:
        return CommentReviewDecision(False, "already_replied", 0)

    if matches_active_campaign:
        return CommentReviewDecision(False, "automated_campaign", 0)

    if BUSINESS_RE.search(text):
        return CommentReviewDecision(True, "business_intent", 90)

    if SKEPTICAL_OR_SARCASTIC_RE.search(text):
        return CommentReviewDecision(True, "skeptical_or_sarcastic", 70)

    if REQUEST_RE.search(text):
        return CommentReviewDecision(True, "direct_request", 80)

    if _looks_like_short_keyword(text, normalized):
        return CommentReviewDecision(True, "possible_unconfigured_keyword", 40)

    return CommentReviewDecision(False, "noise", 0)


def _looks_like_short_keyword(text: str, normalized: str) -> bool:
    cleaned = normalized.strip(" #!?.:,;🙏🙂😀😅🔥👏❤️👍")
    if not cleaned or cleaned in NOISE:
        return False
    words = re.findall(r"[\w'’.-]+", text, re.UNICODE)
    if not (1 <= len(words) <= 3):
        return False
    if len(" ".join(words)) > 35:
        return False
    if all(len(word) <= 2 for word in words):
        return False
    return True


def _normalize(text: str) -> str:
    without_accents = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", without_accents.lower()).strip()
