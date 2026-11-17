#!/usr/bin/env python3
"""Words per extracted claim, measured on EvalKit's own committed extraction output (no API
calls). The ratio behind `evalkit.cost.WORDS_PER_CLAIM`, which counts claims from texts that
already exist (fraud-review-bedrock, D-61 rule 3).

    uv run python scripts/measure_words_per_claim.py           # print the measurement
    uv run python scripts/measure_words_per_claim.py --write   # also write the record

Sources: the failure explorer of the published 200-case run (docs/results/report.html: each
failed answer with its extracted claims) and the demo replay (demo/replay.json: 24 answers
with their claim counts). Words are counted with `evalkit.cost.count_words`.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "results" / "report.html"
REPLAY = ROOT / "demo" / "replay.json"
OUT = ROOT / "docs" / "measurements" / "words-per-claim-2026-11-17.json"

_DETAILS = re.compile(
    r"<details data-model=\"[^\"]+\"[^>]*><summary>([^<]*)</summary>(.*?)</details>", re.S
)
_ANSWER = re.compile(r"<p><strong>Answer\.</strong> (.*?)</p>", re.S)
_TBODY = re.compile(r"<tbody>(.*?)</tbody>", re.S)


def report_answers(text: str) -> list[tuple[str, int]]:
    """(answer text, number of extracted claims) per failure-explorer entry."""
    out = []
    for m in _DETAILS.finditer(text):
        answer = _ANSWER.search(m.group(2))
        if not answer:
            continue
        body = _TBODY.search(m.group(2))
        n = body.group(1).count("<tr>") if body else 0
        out.append((html.unescape(re.sub(r"<[^>]+>", "", answer.group(1))), n))
    return out


def replay_answers(data: dict) -> list[tuple[str, int]]:
    return [(e["answer"], e["n_claims"]) for entries in data.values() for e in entries]


def summarize(pairs: list[tuple[str, int]]) -> dict:
    from evalkit.cost import count_words

    rows = [(count_words(a), n) for a, n in pairs if n > 0]
    words = sum(w for w, _ in rows)
    claims = sum(n for _, n in rows)
    ratios = sorted(w / n for w, n in rows)
    q = statistics.quantiles(ratios, n=4) if len(ratios) > 1 else ratios * 3
    return {
        "answers": len(pairs),
        "answers_with_claims": len(rows),
        "words": words,
        "claims": claims,
        "claims_per_answer": round(claims / len(rows), 3),
        "words_per_claim": round(words / claims, 3),
        "per_answer_words_per_claim_quartiles": [round(x, 2) for x in q],
    }


def measure() -> dict:
    from evalkit.cost import WORDS_PER_CLAIM

    report = summarize(report_answers(REPORT.read_text(encoding="utf-8")))
    replay = summarize(replay_answers(json.loads(REPLAY.read_text(encoding="utf-8"))))
    return {
        "sources": {
            "docs/results/report.html": "failure explorer of the published 200-case run "
            "r-e8dac378d2d3 (failed answers only; 26 had no claims and are left out)",
            "demo/replay.json": "the demo's 24 cached answers (6 abstracts x 4 models)",
        },
        "word_count": "evalkit.cost.count_words (whitespace words; each CJK character is one)",
        "report": report,
        "replay": replay,
        "planning_words_per_claim": WORDS_PER_CLAIM,
        "why": "below the report's lower quartile of per-answer words per claim, so a text is "
        "planned at more claims than a typical answer of its length holds",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help=f"write {OUT.relative_to(ROOT)}")
    args = ap.parse_args(argv)
    result = measure()
    text = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    print(text, end="")
    if args.write:
        OUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
