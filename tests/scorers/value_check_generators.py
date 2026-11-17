"""False-claim generators from the B5.13 verifier passes, ported for B5.16 (D-74).

`gen1()` is verify513f/gen.py (pass 6: named fields, joins, comparison words; 10,080 claims on
one record). `gen2(seed, n_records)` is verify513g/gen2.py (pass 7: paraphrases,
parentheticals, approximate words, comparisons, ranges, multiples, percent and unit scaling,
hour formats, lists, "respectively", unnamed subjects, mixed true/false; about 141 claims per
record). Every claim is false for its record: none may be `verified`.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator
from typing import Any

Claim = tuple[str, str, dict[str, Any]]  # (family, text, record)

GEN1_RECORD = {
    "amount": 812.4,
    "amt_vs_usual": 9.1,
    "hour": 3,
    "card_history_n": 40,
    "risk_score": 0.83,
    "category_freq": 0.05,
}


def gen1() -> Iterator[Claim]:
    rec = GEN1_RECORD
    words = {
        "amount": ["the amount", "amount"],
        "risk_score": ["the risk score", "risk"],
        "card_history_n": ["card history", "the card history"],
        "hour": ["the hour"],
    }
    cmps = {">": ["above", "over", "more than", "exceeds"], "<": ["below", "under", "less than"]}
    joins = [" with ", " and ", " while ", ", and ", ", like ", " compared with ", " next to "]
    joins += [" unlike "]
    for t, d in itertools.product(words, words):
        if t == d:
            continue
        for op in cmps:
            v = rec[t]
            x = round(v * 2 + 1, 2) if op == ">" else round(v / 3, 3)
            for cw, j, tw, dw in itertools.product(cmps[op], joins, words[t], words[d]):
                for s in (
                    f"{dw.capitalize()} is high{j}{tw} {cw} {x}",
                    f"{dw.capitalize()}{j}{cw} {x} for {tw}",
                    f"{cw.capitalize()} {x} on {tw}{j}{dw} noted",
                    f"{tw.capitalize()}, unlike {dw}, is {cw} {x}",
                    f"{tw.capitalize()} of the {dw.split()[-1]} is {cw} {x}",
                ):
                    yield "gen1", s, rec


NAMES = {
    "amount": ["amount", "Amount", "AMOUNT", "the amount", "amounts", "the purchase amount"],
    "risk_score": [
        "risk score",
        "Risk Score",
        "RISK SCORE",
        "risk-score",
        "risk_score",
        "Risk_Score",
        "risk  score",
        "the risk scores",
        "risk score's value",
    ],
    "card_history_n": [
        "card history",
        "Card History",
        "card-history",
        "card_history_n",
        "CARD HISTORY",
        "the card histories",
    ],
    "amt_vs_usual": [
        "amt vs usual",
        "amt_vs_usual",
        "AMT VS USUAL",
        "amt-vs-usual",
        "Amt Vs Usual",
    ],
    "category_freq": [
        "category freq",
        "category_freq",
        "Category Freq",
        "CATEGORY FREQ",
        "category-freq",
    ],
    "hour": ["hour", "Hour", "HOUR", "the hour", "hours"],
}
FRACTION = {"risk_score", "category_freq"}
INTS = {"hour", "card_history_n"}
FIELDS = list(NAMES)
AFTER = ["", " overall", " for this alert", " on this purchase"]
APPROX = ["about", "roughly", "nearly", "around", "approximately", "close to", "some"]
APPROX += ["just about", "~"]
VERBS = ["is", "was", "came in at", "stood at", "sits at", "reads", "equals", "measured"]
VERBS += ["registers at", "of"]
UNNAMED = {
    "amount": ["price", "charge", "total", "transaction value", "amout", "ticket size", "spend"],
    "card_history_n": [
        "number of earlier purchases",
        "prior purchase count",
        "card's history count",
        "card has",
    ],
    "hour": ["time", "purchase time", "clock time", "huor"],
    "risk_score": ["fraud probability", "riskscore", "RiskScore", "model score"],
    "amt_vs_usual": ["spend multiple", "AmtVsUsual", "spend ratio"],
}


def _far(v: float, x: float, s: str | None = None) -> bool:
    """x is clearly not v: more than 30% off and not equal at display precision."""
    if s is not None and "." in s:
        d = len(s.split(".")[1].rstrip("%"))
        if round(v, d) == round(x, d) or abs(v - x) <= 10**-d:
            return False
    elif s is not None and abs(v - x) <= 1:
        return False
    return abs(x - v) > 0.3 * max(abs(v), abs(x)) + 1e-9


def _fmt(f: str, x: float) -> str:
    if f in INTS:
        return str(round(x))
    if f in FRACTION:
        return f"{x:.3g}" if x >= 0.001 else f"{x:.2e}"
    return f"{x:.2f}" if f == "amount" else f"{x:.1f}"


def gen2(seed: int, n_records: int = 400) -> Iterator[Claim]:
    r = random.Random(seed)

    def record() -> dict[str, Any]:
        rec: dict[str, Any] = {
            "amount": round(r.uniform(5, 3000), 2),
            "amt_vs_usual": round(r.uniform(0.1, 40), 3),
            "hour": r.randrange(24),
            "card_history_n": r.randrange(0, 5000),
            "risk_score": round(r.uniform(0.001, 0.999), 4),
            "category_freq": round(r.uniform(0.001, 0.4), 4),
        }
        if r.random() < 0.4:
            rec["velocity_ratio"] = round(r.uniform(0.5, 20), 2)
        if r.random() < 0.3:
            rec["merchant_age_days"] = r.randrange(1, 4000)
        return rec

    def false_val(rec: dict[str, Any], f: str) -> tuple[float, str]:
        v = rec[f]
        for _ in range(100):
            k = r.choice([0.2, 0.4, 0.55, 1.6, 2.2, 3.5, 6])
            x = v * k if v else r.choice([7, 15, 40])
            if f == "hour":
                x = r.randrange(24)
            if f in FRACTION:
                x = min(x, 0.99)
            if f == "card_history_n":
                x = round(x)
            s = _fmt(f, x)
            if _far(v, float(s), s) and float(s) > 0:
                return float(s), s
        raise RuntimeError("no false value")

    def nm(f: str) -> str:
        return r.choice(NAMES[f])

    def cap(s: str) -> str:
        return s[0].upper() + s[1:]

    for _ in range(n_records):
        rec = record()
        for f in FIELDS:
            v = rec[f]
            for _rep in range(3):
                _, s = false_val(rec, f)
                n = nm(f)
                yield "F1_paraphrase", r.choice([
                    f"{cap(n)} {r.choice(VERBS)} {s}{r.choice(AFTER)}.",
                    f"We note {n} of {s}.",
                    f"{s} is the {n}.",
                    f"At {s}, the {n} stands out.",
                    f"Looking at {n}, it is {s}.",
                    f"The alert shows {n} {s}, worth a look.",
                    f"With {n} at {s}, a person should review it.",
                    f"{cap(n)}: {s}",
                    f"{cap(n)} = {s}",
                ]), rec  # fmt: skip
                yield "F2_parenthetical", r.choice([
                    f"The alert ({n} {s}) needs review.",
                    f"{cap(n)} (reported as {s}) is notable.",
                    f"{cap(n)} — {s} — is notable.",
                    f"Review it: {n} [{s}] matters.",
                    f"The {n}, at {s}, matters.",
                    f"{cap(n)} ({s}, per the record) is high.",
                ]), rec  # fmt: skip
                yield "F7_approx", f"{cap(n)} is {r.choice(APPROX)} {s}.", rec
                up = float(_fmt(f, v * r.choice([1.5, 2, 3]) + (1 if f in INTS else 0)))
                dn = float(_fmt(f, v * r.choice([0.3, 0.5, 0.6])))
                us, ds = _fmt(f, up), _fmt(f, dn)
                if up > v and _far(v, up, us):
                    yield "F12_cmp_false", r.choice([
                        f"{cap(n)} is above {us}.", f"{cap(n)} exceeds {us}.",
                        f"{cap(n)} is greater than or equal to {us}.", f"{cap(n)} >= {us}",
                        f"{cap(n)} > {us}", f"{cap(n)} is {us} or higher.",
                        f"{cap(n)} is at least {us}.", f"{cap(n)} is not below {us}.",
                        f"{cap(n)} isn't under {us}.", f"{cap(n)} is in excess of {us}.",
                        f"{cap(n)} tops {us}.", f"{cap(n)} is north of {us}.", f"{cap(n)} ≥ {us}",
                        f"{cap(n)} of {us} or more", f"Above {us} for {n}.",
                        f"Over {us}: the {n}.",
                    ]), rec  # fmt: skip
                if 0 < dn < v and _far(v, dn, ds):
                    yield "F12_cmp_false", r.choice([
                        f"{cap(n)} is below {ds}.", f"{cap(n)} is under {ds}.",
                        f"{cap(n)} is less than {ds}.", f"{cap(n)} <= {ds}", f"{cap(n)} < {ds}",
                        f"{cap(n)} is {ds} or less.", f"{cap(n)} is at most {ds}.",
                        f"{cap(n)} is not above {ds}.", f"{cap(n)} doesn't exceed {ds}.",
                        f"{cap(n)} is no more than {ds}.", f"{cap(n)} is shy of {ds}.",
                        f"{cap(n)} falls short of {ds}.", f"{cap(n)} ≤ {ds}",
                        f"{cap(n)} up to {ds}",
                    ]), rec  # fmt: skip
                if f not in INTS or v > 3:
                    if r.random() < 0.5:
                        lo, hi = up, float(_fmt(f, up * 1.5 + 1))
                    else:
                        lo, hi = float(_fmt(f, dn * 0.5)), dn
                    ok_lo, ok_hi = _far(v, lo, _fmt(f, lo)), _far(v, hi, _fmt(f, hi))
                    if hi > lo and not (lo <= v <= hi) and ok_lo and ok_hi:
                        a, b = _fmt(f, lo), _fmt(f, hi)
                        yield "F6_range", r.choice([
                            f"{cap(n)} is between {a} and {b}.",
                            f"{cap(n)} in the {a}–{b} range.",
                            f"{cap(n)} ranges from {a} to {b}.",
                            f"{cap(n)} lies within {a} to {b}.",
                            f"{cap(n)} of {a}-{b}",
                            f"{cap(n)} falls between {a} and {b} (inclusive).",
                        ]), rec  # fmt: skip
            if f == "amt_vs_usual":
                for _rep in range(3):
                    _, s = false_val(rec, f)
                    yield "F5_multiple", r.choice([
                        f"Amount is {s}x the card's usual.",
                        f"Amount is {s} times the card's usual.",
                        f"Spend is {s}-fold the usual.", f"A {s}X jump over usual spend.",
                        f"{cap(nm(f))} is {s} times.", f"The purchase is {s} × usual.",
                        f"{s} times the usual amount was spent.", f"Spend jumped {s}-fold.",
                    ]), rec  # fmt: skip
            if f in FRACTION:
                pv = v * 100
                small = v < 0.05
                for s in (
                    f"{v:.3f}%" if small else f"{v:.2f}%",
                    f"{v:.3f} percent" if small else f"{v:.2f} percent",
                ):
                    yield "F4_pct_misscale", f"{cap(nm(f))} is {s}.", rec
                x = pv * r.choice([0.3, 2.5])
                if _far(pv, x, f"{x:.0f}") and x < 100:
                    yield "F4_pct_wrong", f"{cap(nm(f))} is {x:.0f}%.", rec
                    yield "F4_pct_wrong", f"{cap(nm(f))} of {x:.0f} per cent.", rec
            if f in ("card_history_n", "amount", "amt_vs_usual"):
                yield "F4_unit_misscale", f"{cap(nm(f))} is {_fmt(f, v)}%.", rec
            if f == "amount":
                yield "F4_unit_misscale", f"The amount is {v:.2f} cents.", rec
                yield "F4_unit_misscale", f"Amount of {v:.2f} thousand dollars.", rec
                yield "F4_unit_misscale", f"Amount was {v * 100:.0f} dollars.", rec
        h = rec["hour"]
        wrong = (h + r.choice([3, 5, 8, 12])) % 24
        h12 = h % 12 or 12
        ap = "p.m." if h < 12 else "a.m."
        part, eve = ("afternoon", "evening") if h < 12 else ("morning", "morning")
        for t in (
            f"Purchase hour was {wrong}:00.",
            f"The hour is the {wrong}th.",
            f"Hour {wrong}.",
            f"It happened in hour {wrong}.",
            f"Purchase at {h12} {ap}, per the hour field.",
            f"The hour was {h12} {ap}",
            f"Hour: {h12}{ap.replace('.', '')}",
            f"Bought at {h12} o'clock in the {part}.",
            f"Purchase at {h12} in the {eve}.",
            f"The hour was {wrong} o'clock.",
        ):
            yield "F8_hour", t, rec
        fs = r.sample(FIELDS, 3)
        items = [(nm(f), _fmt(f, rec[f]) if i else false_val(rec, f)[1]) for i, f in enumerate(fs)]
        r.shuffle(items)
        yield "F9_list_one_false", r.choice([
            "Signals: " + ", ".join(f"{a} {b}" for a, b in items) + ".",
            "; ".join(f"{cap(a)} {b}" for a, b in items),
            "- " + "\n- ".join(f"{a}: {b}" for a, b in items),
            f"{cap(items[0][0])} {items[0][1]}, {items[1][0]} {items[1][1]} and "
            f"{items[2][0]} {items[2][1]}.",
        ]), rec  # fmt: skip
        for k in (2, 3):
            fs = r.sample(FIELDS, k)
            vals = [_fmt(f, rec[f]) for f in fs]
            perm = vals[1:] + vals[:1]
            if all(_far(rec[f], float(p), p) for f, p in zip(fs, perm, strict=True)):
                names = [nm(f) for f in fs]
                head = cap(", ".join(names[:-1]))
                yield "F10_respectively", r.choice([
                    f"{head} and {names[-1]} are {', '.join(perm[:-1])} and {perm[-1]}.",
                    f"{head} and {names[-1]} are {', '.join(perm[:-1])} and {perm[-1]} "
                    "respectively.",
                    f"{cap(' / '.join(names))}: {' / '.join(perm)}",
                ]), rec  # fmt: skip
        for _rep in range(4):
            tgt, src = r.sample(
                ["amount", "card_history_n", "hour", "risk_score", "amt_vs_usual"], 2
            )
            s = _fmt(src, rec[src])
            if not _far(rec[tgt], float(s), s):
                continue
            w = r.choice(UNNAMED[tgt])
            if w == "card has":
                t = f"The card has {s} earlier purchases."
            else:
                t = r.choice([f"The {w} was {s}.", f"{cap(w)}: {s}.", f"{cap(w)} is {s}."])
            yield "F11_unnamed_subject", t, rec
        a, b = r.sample(FIELDS, 2)
        join = r.choice(["and", "while", "with", ", and", ", whereas"])
        yield (
            "F13_mixed",
            f"{cap(nm(a))} is {_fmt(a, rec[a])} {join} {nm(b)} is {false_val(rec, b)[1]}.",
            rec,
        )
