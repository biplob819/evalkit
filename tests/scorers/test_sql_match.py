import json
from pathlib import Path

import pytest

from evalkit.schemas import ResultSet
from evalkit.scorers import sql_match

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "sql_match_cases.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_sql_match_golden_cases(case):
    pred = ResultSet(**case["predicted"])
    gold = ResultSet(**case["gold"])
    result = sql_match(pred, gold)
    assert result.value == case["expected"], result.details
    if result.value == 0.0:
        assert result.details["reason"] in {
            "column_count",
            "row_count",
            "row_mismatch",
            "rows_differ",
        }


@pytest.mark.contract
def test_fixture_covers_the_named_behaviours_and_export():
    names = {c["name"] for c in CASES}
    assert len(names) >= 12
    for required in (
        "alias_positional",
        "column_permutation_by_name",
        "duplicates_matter",
        "row_order_required",
        "float_tolerance",
        "int_vs_float",
        "none_equals_none",
        "empty_matches_empty",
        "extra_row",
        "missing_column",
    ):
        assert required in names, required
    import evalkit.scorers as s

    assert "sql_match" in s.__all__


def test_sql_match_is_deterministic_and_symmetric_for_unordered_sets():
    a = ResultSet(columns=("x", "y"), rows=((1, "a"), (2, "b"), (2, "b")))
    b = ResultSet(columns=("y", "x"), rows=(("b", 2), ("a", 1), ("b", 2)))
    assert sql_match(a, b) == sql_match(a, b)
    assert sql_match(a, b).value == sql_match(b, a).value == 1.0


def test_tolerances_are_configurable():
    p = ResultSet(columns=("v",), rows=((1.01,),))
    g = ResultSet(columns=("v",), rows=((1.0,),))
    assert sql_match(p, g).value == 0.0
    assert sql_match(p, g, rel_tol=0.02).value == 1.0
