"""Result-set comparison for analytics answers (SQL-01). A plain function, not a
(case, completion) Scorer, because it compares two result sets.

Unordered matching is exact where it can be and tolerant only where it must be:
1. rows identical after normalisation are paired off with a multiset count;
2. the remaining rows are grouped by their non-numeric values (those must match exactly);
3. inside a group, numeric columns are paired by a maximum bipartite matching whose edges
   are pairs within tolerance on every numeric column. Candidate edges come from a window on
   the first numeric column, so the usual case is close to n log n rather than n squared.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from collections.abc import Collection
from decimal import Decimal
from typing import Any

from evalkit.schemas import ResultSet
from evalkit.scorers.base import ScoreResult
from evalkit.scorers.sql_equivalences import check_names, variants

Norm = tuple[str, Any]


def _norm_value(v: Any) -> Norm:
    if isinstance(v, bool):
        return ("b", v)
    if v is None:
        return ("n", None)
    if isinstance(v, int):
        return ("num", v)
    if isinstance(v, float | Decimal):
        f = float(v)
        return ("num", f) if math.isfinite(f) else ("x", repr(v))
    if isinstance(v, str):
        return ("s", v.rstrip())
    return ("o", repr(v))


def _close(a: float | int, b: float | int, rel_tol: float, abs_tol: float) -> bool:
    diff = abs(a - b)  # exact for ints, so huge integers are never rounded together
    return diff <= abs_tol or diff <= rel_tol * max(abs(a), abs(b))


def _window(v: float | int, rel_tol: float, abs_tol: float) -> float:
    """Half-width w such that every b with _close(v, b) satisfies |v - b| <= w."""
    rel_part = rel_tol * abs(v) / (1 - rel_tol) if rel_tol < 1 else math.inf
    return max(abs_tol, rel_part)


def _max_matching(adj: list[list[int]], n_right: int) -> int:
    """Hopcroft-Karp maximum bipartite matching size (iterative, no recursion limit)."""
    n_left = len(adj)
    match_l = [-1] * n_left
    match_r = [-1] * n_right
    size = 0
    while True:
        dist = [-1] * n_left
        queue = [u for u in range(n_left) if match_l[u] == -1]
        for u in queue:
            dist[u] = 0
        found = False
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            for v in adj[u]:
                w = match_r[v]
                if w == -1:
                    found = True
                elif dist[w] == -1:
                    dist[w] = dist[u] + 1
                    queue.append(w)
        if not found:
            return size
        for start in range(n_left):
            if match_l[start] != -1:
                continue
            # iterative DFS along layered graph
            stack = [(start, iter(adj[start]))]
            path: list[tuple[int, int]] = []
            while stack:
                u, it = stack[-1]
                advanced = False
                for v in it:
                    w = match_r[v]
                    if w == -1:
                        path.append((u, v))
                        for pu, pv in path:
                            match_l[pu], match_r[pv] = pv, pu
                        size += 1
                        stack.clear()
                        advanced = True
                        break
                    if dist[w] == dist[u] + 1:
                        path.append((u, v))
                        stack.append((w, iter(adj[w])))
                        advanced = True
                        break
                if not advanced and stack:
                    stack.pop()
                    dist[u] = -2  # dead end this phase
                    if path:
                        path.pop()


def _group_matches(
    pred: list[tuple[Norm, ...]],
    gold: list[tuple[Norm, ...]],
    num_idx: list[int],
    rel_tol: float,
    abs_tol: float,
) -> bool:
    if len(pred) != len(gold):
        return False
    if not num_idx:
        return Counter(pred) == Counter(gold)

    def nums(row: tuple[Norm, ...]) -> tuple[float | int, ...]:
        return tuple(row[i][1] for i in num_idx)

    def row_close(a: tuple[float | int, ...], b: tuple[float | int, ...]) -> bool:
        return all(_close(x, y, rel_tol, abs_tol) for x, y in zip(a, b, strict=True))

    p_nums = sorted(nums(r) for r in pred)
    g_nums = sorted(nums(r) for r in gold)
    if all(row_close(a, b) for a, b in zip(p_nums, g_nums, strict=True)):
        return True  # sorted pairing works: the common case
    keys = [g[0] for g in g_nums]
    adj: list[list[int]] = []
    for a in p_nums:
        w = _window(a[0], rel_tol, abs_tol)
        lo, hi = bisect.bisect_left(keys, a[0] - w), bisect.bisect_right(keys, a[0] + w)
        adj.append([j for j in range(lo, hi) if row_close(a, g_nums[j])])
    return _max_matching(adj, len(g_nums)) == len(p_nums)


MAX_ASSIGNMENTS = 200  # column assignments tried when matching gold columns by value


def _column_profile(rows: Any, i: int, rel_tol: float, abs_tol: float) -> tuple[Any, ...]:
    """A column's values as a sorted, comparable profile (numbers rounded to the tolerance's
    scale so near-equal columns profile alike; used only to prune assignments)."""
    vals = [_norm_value(r[i]) for r in rows]
    out = []
    for kind, v in vals:
        if kind == "num":
            out.append(("num", round(float(v), 4)))
        else:
            out.append((kind, str(v)))
    return tuple(sorted(out))


def _assignments(
    predicted: ResultSet, gold: ResultSet, rel_tol: float, abs_tol: float
) -> list[list[int]]:
    """Ways to map each gold column to a distinct predicted column: by name when every gold
    name is present, else by matching value profiles."""
    pcols = [c.casefold() for c in predicted.columns]
    gcols = [c.casefold() for c in gold.columns]
    if set(gcols) <= set(pcols) and len(set(pcols)) == len(pcols):
        return [[pcols.index(c) for c in gcols]]
    gprof = [_column_profile(gold.rows, j, rel_tol, abs_tol) for j in range(len(gcols))]
    pprof = [_column_profile(predicted.rows, i, rel_tol, abs_tol) for i in range(len(pcols))]
    cands = [[i for i in range(len(pcols)) if pprof[i] == gprof[j]] for j in range(len(gcols))]
    out: list[list[int]] = []

    def walk(j: int, used: list[int]) -> None:
        if len(out) >= MAX_ASSIGNMENTS:
            return
        if j == len(gcols):
            out.append(list(used))
            return
        for i in cands[j]:
            if i not in used:
                used.append(i)
                walk(j + 1, used)
                used.pop()

    walk(0, [])
    return out


def sql_match(
    predicted: ResultSet,
    gold: ResultSet,
    *,
    rel_tol: float = 1e-6,
    abs_tol: float = 1e-9,
    allow_extra_columns: bool = False,
    question: str | None = None,
    equivalences: Collection[str] = (),
) -> ScoreResult:
    """1.0 if `predicted` returns the same rows and values as `gold`, else 0.0 with a reason.

    With `allow_extra_columns` (the agent track, D-55), a table with more columns than the gold
    passes when some choice of its columns (matched by name, else by values) reproduces the
    gold exactly; extra or missing rows still fail.

    `equivalences` (B5.15, D-71) names opt-in shape equivalences, `percent_scale`, `pivot`,
    `empty_check` and `period_number` (`sql_equivalences.EQUIVALENCES`, rules there), and
    `question` is the question text two of them read. None is on by default, and with none
    named the result is exactly the strict one. With any named, the strict match runs first;
    only when it fails is each named reshaping of `predicted` matched strictly in turn, and
    `details` gains `correct_strict`, the `equivalences` that matched (empty when none did or
    none was needed) and, on a rescue, `strict_reason`."""
    if not (0 <= rel_tol < 1) or abs_tol < 0:
        raise ValueError("rel_tol must be in [0, 1) and abs_tol >= 0")
    if question is not None and not isinstance(question, str):
        raise TypeError("question must be the question text or None")
    names = check_names(equivalences)
    strict = _strict(predicted, gold, rel_tol, abs_tol, allow_extra_columns)
    if not names:
        return strict
    if strict.value == 1.0:
        return ScoreResult(
            value=1.0, details={**strict.details, "correct_strict": True, "equivalences": []}
        )
    for rules, variant in variants(predicted, gold, names, question, rel_tol, abs_tol):
        res = _strict(variant, gold, rel_tol, abs_tol, allow_extra_columns)
        if res.value == 1.0:
            return ScoreResult(
                value=1.0,
                details={
                    **res.details,
                    "correct_strict": False,
                    "equivalences": list(rules),
                    "strict_reason": strict.details["reason"],
                },
            )
    return ScoreResult(
        value=0.0, details={**strict.details, "correct_strict": False, "equivalences": []}
    )


def _strict(
    predicted: ResultSet,
    gold: ResultSet,
    rel_tol: float,
    abs_tol: float,
    allow_extra_columns: bool,
) -> ScoreResult:
    if allow_extra_columns and len(predicted.columns) > len(gold.columns):
        tried = _assignments(predicted, gold, rel_tol, abs_tol)
        for order in tried:
            res = _compare(predicted, gold, order, "subset", rel_tol, abs_tol)
            if res.value == 1.0:
                return res
        return ScoreResult(
            value=0.0,
            details={
                "reason": "no_matching_columns" if not tried else "row_mismatch",
                "predicted": len(predicted.columns),
                "gold": len(gold.columns),
                "assignments_tried": len(tried),
            },
        )
    pcols = [c.casefold() for c in predicted.columns]
    gcols = [c.casefold() for c in gold.columns]
    if sorted(pcols) == sorted(gcols) and len(set(gcols)) == len(gcols):
        order, mode = [pcols.index(c) for c in gcols], "by_name"
    elif len(pcols) == len(gcols):
        order, mode = list(range(len(gcols))), "positional"
    else:
        return ScoreResult(
            value=0.0,
            details={"reason": "column_count", "predicted": len(pcols), "gold": len(gcols)},
        )
    return _compare(predicted, gold, order, mode, rel_tol, abs_tol)


def _compare(
    predicted: ResultSet,
    gold: ResultSet,
    order: list[int],
    mode: str,
    rel_tol: float,
    abs_tol: float,
) -> ScoreResult:
    p_rows = [tuple(_norm_value(r[i]) for i in order) for r in predicted.rows]
    g_rows = [tuple(_norm_value(v) for v in r) for r in gold.rows]
    if len(p_rows) != len(g_rows):
        return ScoreResult(
            value=0.0,
            details={
                "reason": "row_count",
                "predicted": len(p_rows),
                "gold": len(g_rows),
                "columns": mode,
            },
        )

    def same(a: tuple[Norm, ...], b: tuple[Norm, ...]) -> bool:
        for x, y in zip(a, b, strict=True):
            if x[0] != y[0]:
                return False
            if x[0] == "num":
                if not _close(x[1], y[1], rel_tol, abs_tol):
                    return False
            elif x != y:
                return False
        return True

    if gold.ordered:
        for i, (a, b) in enumerate(zip(p_rows, g_rows, strict=True)):
            if not same(a, b):
                return ScoreResult(
                    value=0.0, details={"reason": "row_mismatch", "row": i, "columns": mode}
                )
        return ScoreResult(value=1.0, details={"columns": mode, "ordered": True})

    # 1. exact pairs
    remaining = Counter(g_rows)
    left_p: list[tuple[Norm, ...]] = []
    for r in p_rows:
        if remaining[r] > 0:
            remaining[r] -= 1
        else:
            left_p.append(r)
    left_g = list(remaining.elements())
    if not left_p and not left_g:
        return ScoreResult(value=1.0, details={"columns": mode, "ordered": False})

    # 2. group by non-numeric signature; 3. tolerant matching inside each group
    def signature(row: tuple[Norm, ...]) -> tuple[Any, ...]:
        return tuple("num" if t == "num" else (t, v) for t, v in row)

    groups_p: dict[tuple[Any, ...], list[tuple[Norm, ...]]] = defaultdict(list)
    groups_g: dict[tuple[Any, ...], list[tuple[Norm, ...]]] = defaultdict(list)
    for r in left_p:
        groups_p[signature(r)].append(r)
    for r in left_g:
        groups_g[signature(r)].append(r)
    for sig in set(groups_p) | set(groups_g):
        num_idx = [i for i, part in enumerate(sig) if part == "num"]
        if not _group_matches(
            groups_p.get(sig, []), groups_g.get(sig, []), num_idx, rel_tol, abs_tol
        ):
            return ScoreResult(
                value=0.0,
                details={
                    "reason": "rows_differ",
                    "columns": mode,
                    "unmatched_predicted": len(groups_p.get(sig, [])),
                    "unmatched_gold": len(groups_g.get(sig, [])),
                    "example_gold_row": [v for _, v in (groups_g.get(sig) or groups_p[sig])[0]],
                },
            )
    return ScoreResult(value=1.0, details={"columns": mode, "ordered": False})
