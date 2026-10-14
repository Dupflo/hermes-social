"""s23 — the review processor, versioned byte for byte from the production copy (console ADR 054, AC 7).

The file is only ever read as bytes here: no test imports it, runs it, or hands it to runpy (it sends real
TikTok messages when run).
"""

import hashlib
import re
from pathlib import Path

PROCESSOR = Path(__file__).resolve().parents[1] / "scripts" / "process_tiktok_review_one.py"
PROCESSOR_SHA256 = "08e5f29d0c87bd154588df5e603d0a5132814d3a420f03e742e39d9aa2892f93"
PROCESSOR_SIZE = 29_522
NAME = "process_tiktok_review_one"


def test_processor_bytes_are_the_production_copy():
    data = PROCESSOR.read_bytes()
    assert len(data) == PROCESSOR_SIZE
    assert hashlib.sha256(data).hexdigest() == PROCESSOR_SHA256


def test_no_test_imports_or_runs_the_processor():
    here = Path(__file__).resolve()
    offenders = []
    for path in sorted(here.parent.glob("*.py")):
        if path == here:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(rf"^\s*(import\s+{NAME}\b|from\s+{NAME}\b)", text, re.M):
            offenders.append(f"{path.name}: import")
        for line in text.splitlines():
            if "spec_from_file_location(" in line and NAME in line:
                offenders.append(f"{path.name}: spec_from_file_location")
        if NAME in text and "runpy" in text:
            offenders.append(f"{path.name}: runpy")
    assert offenders == []
