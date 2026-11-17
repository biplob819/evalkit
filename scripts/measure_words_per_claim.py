#!/usr/bin/env python3
"""Words and characters per extracted claim, measured on EvalKit's own committed extraction
output (no API calls). The ratios behind `evalkit.cost.WORDS_PER_CLAIM` and `CHARS_PER_CLAIM`,
two of the lower bounds that count claims from texts that already exist (D-76).

    uv run python scripts/measure_words_per_claim.py           # print the measurement
    uv run python scripts/measure_words_per_claim.py --write   # also write the record

Sources: the failure explorer of the published 200-case run (docs/results/report.html: each
failed answer with its extracted claims) and the demo replay (demo/replay.json: 24 answers
with their claim counts). Words are counted with `evalkit.cost.count_words`, characters with
`evalkit.cost.count_chars` (non-space, after NFKC). Each ratio is planned at the report's
10th percentile of per-answer ratios, rounded down. The record also checks the whole rule
(`evalkit.cost.claims_in_text`) against the true counts of the report, the replay and the
synthetic fraud-review-shaped fixture (tests/fixtures/faithfulness/fraud_review_synthetic.json),
next to the first D-76 rule (words / 10, at least 4.5).
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import statistics
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "results" / "report.html"
REPLAY = ROOT / "demo" / "replay.json"
FRAUD = ROOT / "tests" / "fixtures" / "faithfulness" / "fraud_review_synthetic.json"
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


def fraud_answers(data: dict) -> list[tuple[str, int]]:
    return [(r["explanation"], len(r["claims"])) for r in data["items"]]


def _p10(ratios: list[float]) -> float:
    return statistics.quantiles(ratios, n=10)[0] if len(ratios) > 1 else ratios[0]


def summarize(pairs: list[tuple[str, int]]) -> dict:
    from evalkit.cost import count_chars, count_words

    rows = [(count_words(a), count_chars(a), n) for a, n in pairs if n > 0]
    words = sum(w for w, _, _ in rows)
    chars = sum(c for _, c, _ in rows)
    claims = sum(n for _, _, n in rows)
    ratios = sorted(w / n for w, _, n in rows)
    q = statistics.quantiles(ratios, n=4) if len(ratios) > 1 else ratios * 3
    return {
        "answers": len(pairs),
        "answers_with_claims": len(rows),
        "words": words,
        "chars": chars,
        "claims": claims,
        "claims_per_answer": round(claims / len(rows), 3),
        "words_per_claim": round(words / claims, 3),
        "chars_per_claim": round(chars / claims, 3),
        "per_answer_words_per_claim_quartiles": [round(x, 2) for x in q],
        "per_answer_words_per_claim_p10": round(_p10(ratios), 2),
        "per_answer_chars_per_claim_p10": round(_p10(sorted(c / n for _, c, n in rows)), 2),
    }


def check_plan(pairs: list[tuple[str, int]], plan: Callable[[str], float]) -> dict:
    """Planned against true claims under a counting rule: the planned total over the true
    total, and the share of answers planned below their true count."""
    rows = [(plan(a), n) for a, n in pairs if n > 0]
    true = sum(n for _, n in rows)
    planned = math.fsum(p for p, _ in rows)
    return {
        "true_claims": true,
        "planned_claims": round(planned, 1),
        "planned_over_true": round(planned / true, 3),
        "under_planned_share": round(sum(p < n for p, n in rows) / len(rows), 3),
    }


def first_rule(text: str) -> float:
    """The first D-76 rule, for comparison: words / 10, at least the planning default."""
    from evalkit.cost import count_words

    return max(count_words(text) / 10, 4.5)


def measure() -> dict:
    from evalkit.cost import CHARS_PER_CLAIM, WORDS_PER_CLAIM, claims_in_text

    data = {
        "report": report_answers(REPORT.read_text(encoding="utf-8")),
        "replay": replay_answers(json.loads(REPLAY.read_text(encoding="utf-8"))),
        "fraud_fixture": fraud_answers(json.loads(FRAUD.read_text(encoding="utf-8"))),
    }
    return {
        "sources": {
            "docs/results/report.html": "failure explorer of the published 200-case run "
            "r-e8dac378d2d3 (failed answers only; 26 had no claims and are left out)",
            "demo/replay.json": "the demo's 24 cached answers (6 abstracts x 4 models)",
            "tests/fixtures/faithfulness/fraud_review_synthetic.json": "SYNTHETIC "
            "fraud-review-shaped explanations, 8 items x 12 claims by construction",
        },
        "word_count": "evalkit.cost.count_words (whitespace words; each CJK character is one)",
        "char_count": "evalkit.cost.count_chars (non-space characters after NFKC)",
        **{name: summarize(pairs) for name, pairs in data.items()},
        "planning_words_per_claim": WORDS_PER_CLAIM,
        "planning_chars_per_claim": CHARS_PER_CLAIM,
        "why": "each ratio is the report's 10th percentile of per-answer ratios, rounded "
        "down, so fewer than one answer in ten is denser than planned by length alone; each "
        "text is planned at the largest of words / ratio, sentence-like units, key=value "
        "fields, distinct numbers, characters / ratio and the planning default 4.5",
        "plan_check": {name: check_plan(pairs, claims_in_text) for name, pairs in data.items()},
        "plan_check_first_rule": {
            name: check_plan(pairs, first_rule) for name, pairs in data.items()
        },
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
