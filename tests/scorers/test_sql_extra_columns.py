"""sql_match with allow_extra_columns (D-55, the agent track)."""

from evalkit.schemas import ResultSet
from evalkit.scorers import sql_match

# ---- allow_extra_columns (D-55, the agent track) ----------------------------------------------

GOLD_RATE = ResultSet(
    columns=("category", "return_rate_pct"), rows=(("Accessories", 4.7), ("Backpacks", 4.1))
)


def test_extra_columns_pass_when_the_gold_columns_are_present_by_value():
    agent = ResultSet(
        columns=("category", "units_sold", "units_returned", "return_pct"),
        rows=(("Backpacks", 798, 33, 4.1), ("Accessories", 716, 34, 4.7)),
    )
    assert sql_match(agent, GOLD_RATE).value == 0.0  # strict default unchanged
    res = sql_match(agent, GOLD_RATE, allow_extra_columns=True)
    assert res.value == 1.0 and res.details["columns"] == "subset"


def test_extra_columns_pass_by_name_and_respect_order():
    gold = ResultSet(columns=("name", "city"), rows=(("A", "X"), ("B", "Y")), ordered=True)
    agent = ResultSet(columns=("store_id", "name", "city"), rows=((1, "A", "X"), (2, "B", "Y")))
    assert sql_match(agent, gold, allow_extra_columns=True).value == 1.0
    swapped = ResultSet(columns=("store_id", "name", "city"), rows=((2, "B", "Y"), (1, "A", "X")))
    assert sql_match(swapped, gold, allow_extra_columns=True).value == 0.0  # order matters


def test_extra_rows_or_wrong_values_still_fail_with_extra_columns():
    extra_row = ResultSet(
        columns=("category", "n", "rate"),
        rows=(("Accessories", 1, 4.7), ("Backpacks", 2, 4.1), ("Tents", 3, 1.0)),
    )
    assert sql_match(extra_row, GOLD_RATE, allow_extra_columns=True).value == 0.0
    wrong = ResultSet(
        columns=("category", "n", "rate"), rows=(("Accessories", 1, 4.7), ("Backpacks", 2, 9.9))
    )
    res = sql_match(wrong, GOLD_RATE, allow_extra_columns=True)
    assert res.value == 0.0 and res.details["reason"] == "no_matching_columns"


def test_value_matching_tries_every_candidate_column():
    """Two columns with the same values as one gold column: the right one must be found."""
    gold = ResultSet(columns=("a", "b"), rows=((1, 1), (2, 3)))
    agent = ResultSet(columns=("x", "y", "z"), rows=((1, 1, 1), (2, 2, 3)))  # y is a decoy for a
    assert sql_match(agent, gold, allow_extra_columns=True).value == 1.0
