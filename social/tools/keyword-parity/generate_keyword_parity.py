#!/usr/bin/env python3
"""s13 (console ADR 038 §3) — generate the shared keyword parity fixtures.

Loads meta-webhook/app/keyword.py and tiktok-backoffice/app/keyword.py BY PATH (stdlib only,
deterministic, no network) and writes keyword-parity.json, where every expectation is computed
by the real Python matchers. The console copies this file verbatim
(hermes-console test/fixtures/keyword-parity.json) to test its TikTok preview port.

Sets `voicebox-ep10` and `systeme-ep8` are SYNTHETIC: the real keyword lists only live in the
production campaign_rules table (reading them was refused); `boundaries` covers Unicode edges.

Usage: python3 generate_keyword_parity.py [--out PATH]
After any change to a keyword.py: regenerate, copy to the console, update the pinned
fingerprint in both tools' tests/test_keyword_parity.py and in the console's Vitest test.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
DEFAULT_OUT = HERE / "keyword-parity.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


META = _load("s13_meta_keyword", TOOLS / "meta-webhook" / "app" / "keyword.py")
TIKTOK = _load("s13_tiktok_keyword", TOOLS / "tiktok-backoffice" / "app" / "keyword.py")

VOICEBOX_EP10 = [
    "voicebox",
    "voice box",
    "voicebix",
    "voïcebox",
    "voicebôx",
    "voisbox",
    "voixbox",
    "voici box",
    "voicebox ep10",
    "ep10",
    "vb",
    "voicebx",
    "vociebox",
    "voicebox!",
    "boîte à voix",
    "voice-box",
    "voicebox10",
    "la voicebox",
]

SYSTEME_EP8 = ["système", "systeme", "systéme", "systême", "systèmes", "system", "sisteme", "système ep8"]

BOUNDARY_KEYWORDS = ["straße", "strasse", "λόγος", "fiche", "istanbul", "x2", "a_b", "café", "10"]
BOUNDARY_TEXTS = [
    "STRASSE",
    "Straße",
    "STRAẞE",
    "ΛΌΓΟΣ",
    "λόγοσ",
    "ﬁche",
    "İstanbul",
    "x2",
    "a_b",
    "café",
    "café",
    "10€",
    "le10",
    "🔥café",
    "café_",
    "10 ans",
]


def _nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def _templates(kw: str) -> list[str]:
    spaced = kw.replace(" ", "  ") if " " in kw else f"je veux  {kw}"
    newline = kw.replace(" ", "\n") if " " in kw else f"je veux\n{kw}"
    tab = kw.replace(" ", "\t") if " " in kw else f"je veux\t{kw}"
    return [
        kw,
        kw.upper(),
        kw[:1].upper() + kw[1:],
        _nfd(f"je veux {kw}"),
        _nfd(kw),
        f"{kw} ?",
        f"{kw}!",
        f"({kw})",
        f'"{kw}"',
        f"{kw}…",
        f"sans {kw}",
        f"Sans {kw}",
        f"pas de {kw}",
        f"pas  de {kw}",
        f"la{kw}",
        f"{kw}2",
        f"{kw}_",
        f"{kw}-y",
        f"l'{kw}",
        spaced,
        newline,
        tab,
        f"{kw}🔥",
        f"  {kw}  ",
        f"je veux la {kw} stp",
        f"JE VEUX {kw.upper()} !!",
    ]


CROSS_PAIRS = [
    ("voicebix", "voicebox"),
    ("voïcebox", "voicebox"),
    ("voicebox", "voïcebox"),
    ("voicebôx", "voicebox"),
    ("voici  box", "voici box"),
    ("voici box", "voicebox"),
    ("voice box", "voicebox"),
    ("voicebox", "voice box"),
    ("voice-box", "voice box"),
    ("voicebox ep10", "ep10"),
    ("voicebox ep10", "voicebox"),
    ("voicebox10", "voicebox"),
    ("ep10", "voicebox ep10"),
    ("la voicebox", "voicebox"),
    ("sans la voicebox", "voicebox"),
    ("pas de voicebox", "voicebox"),
    ("pas\nde voicebox", "voicebox"),
    ("sans voicebox mais avec la voicebox", "voicebox"),
    ("vb", "voicebox"),
    ("VB stp", "vb"),
    ("boite a voix", "boîte à voix"),
    ("BOÎTE À VOIX", "boîte à voix"),
    ("système", "systeme"),
    ("systeme", "système"),
    ("systéme", "système"),
    ("SYSTÈME", "système"),
    ("systèmes", "système"),
    ("système", "systèmes"),
    ("system", "système"),
    ("le système ep8", "système ep8"),
    ("le système  ep8", "système ep8"),
    ("Système EP8", "système ep8"),
    ("sans système", "système"),
]


def _pair(text: str, keyword: str) -> dict:
    return {
        "text": text,
        "keyword": keyword,
        "meta": bool(META.contains_keyword(text, keyword)),
        "tiktok": bool(TIKTOK.contains_keyword(text, keyword)),
    }


def _campaign_row(text: str, keywords: list[str]) -> dict:
    meta_index = next((i for i, kw in enumerate(keywords) if META.contains_keyword(text, kw)), None)
    # TikTok reads keywords ORDER BY keyword (BINARY = code point order) and keeps the first match.
    tiktok_keyword = next((kw for kw in sorted(keywords) if TIKTOK.contains_keyword(text, kw)), None)
    return {"text": text, "meta_index": meta_index, "tiktok_keyword": tiktok_keyword}


CAMPAIGN_TEXTS = [
    "je veux la voicebox",
    "sans voicebox",
    "la voicebix stp",
    "VOICEBOX EP10 !",
    "voici  box",
    "voici box",
    "c'est quoi ep10",
    "boîte à voix",
    "rien à voir",
    "le système ep8 svp",
    "systeme",
    "pas de système",
    "SYSTÈMES",
    "system",
    "systême ?",
]


def _set(name: str, keywords: list[str], pairs: list[tuple[str, str]], synthetic: bool) -> dict:
    return {
        "name": name,
        "synthetic": synthetic,
        "keywords": keywords,
        "pairs": [_pair(text, keyword) for text, keyword in pairs],
        "campaign": [_campaign_row(text, keywords) for text in CAMPAIGN_TEXTS],
    }


def build() -> dict:
    voicebox_pairs = [(text, kw) for kw in VOICEBOX_EP10 for text in _templates(kw)]
    voicebox_pairs += [p for p in CROSS_PAIRS if p[1] in VOICEBOX_EP10]
    systeme_pairs = [(text, kw) for kw in SYSTEME_EP8 for text in _templates(kw)]
    systeme_pairs += [p for p in CROSS_PAIRS if p[1] in SYSTEME_EP8]
    boundary_pairs = [(text, kw) for kw in BOUNDARY_KEYWORDS for text in BOUNDARY_TEXTS]
    return {
        "version": 1,
        "sets": [
            _set("voicebox-ep10", VOICEBOX_EP10, voicebox_pairs, True),
            _set("systeme-ep8", SYSTEME_EP8, systeme_pairs, True),
            _set("boundaries", BOUNDARY_KEYWORDS, boundary_pairs, True),
        ],
    }


def write(out: Path = DEFAULT_OUT) -> None:
    text = json.dumps(build(), ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    Path(out).write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    write(parser.parse_args().out)


if __name__ == "__main__":
    main()
