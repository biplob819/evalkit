"""B5.7 run history and alarm bands (LED-01, LED-02), across the five proof dimensions.

Synthetic fixtures only (tests/ledger_helpers.py); no model or network call; no `.env`.
Regenerate the golden ledger after an intended change with EVALKIT_UPDATE_GOLDEN=1.
"""

from __future__ import annotations

import json
import os
import random
import statistics
from pathlib import Path

import pytest

from evalkit import cli
from evalkit import heldout as H
from evalkit import ledger as L
from evalkit.gates import parse_gates
from tests.ledger_helpers import (
    FRAUD,
    FRAUD_HISTORY,
    HELD_IDS,
    LEDGER_FIX,
    MANIFEST,
    at,
    commit,
    evalkit_summary,
    fraud_rows,
    fraud_run,
    no_network,  # noqa: F401  (autouse below)
    rate,
    repo,
    write_run,
)

UPDATE = os.environ.get("EVALKIT_UPDATE_GOLDEN") == "1"


@pytest.fixture(autouse=True)
def _offline(no_network):  # noqa: F811
    yield


def _history(tmp: Path, ledger: Path, rows: list[tuple[int, int, float]]) -> list[L.RunCheck]:
    return [L.record_run(ledger, fraud_run(tmp, i, *r), at=at(i + 1)) for i, r in enumerate(rows)]


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


# ==================================== LED-01 run history ====================================


def test_history_add_appends_one_chained_line_and_refuses_a_repeat(tmp_path):
    """logical: one chained line per run; the same summary bytes again exit 2, nothing added."""
    ledger = tmp_path / "runs.jsonl"
    run = fraud_run(tmp_path, 0, 1, 212, 4.1)
    L.record_run(ledger, run, at=at(1))
    rows = _lines(ledger)
    assert len(rows) == 1 and rows[0]["prev"] == H.GENESIS
    L.record_run(ledger, fraud_run(tmp_path, 1, 2, 210, 4.2), at=at(2))
    assert len(_lines(ledger)) == 2 and H.chain_problems(ledger) == []
    before = ledger.read_bytes()
    with pytest.raises(L.LedgerError) as exc:
        L.record_run(ledger, run, at=at(3))
    assert exc.value.code == 2 and "already recorded" in str(exc.value)
    assert ledger.read_bytes() == before
    code = cli.main(["history", "add", str(run), "--ledger", str(ledger)])
    assert code == 2 and ledger.read_bytes() == before


def test_pending_run_exits_4_and_is_not_recorded(tmp_path, capsys):
    """logical: judging pending is refused unrecorded with exit 4 (GAT-09)."""
    folder = tmp_path / "pending"
    folder.mkdir()
    (folder / "summary.json").write_text(
        json.dumps(evalkit_summary("p1", pending=True)), encoding="utf-8"
    )
    ledger = tmp_path / "runs.jsonl"
    with pytest.raises(L.LedgerError) as exc:
        L.record_run(ledger, folder)
    assert exc.value.code == 4
    assert not ledger.exists()
    assert cli.main(["history", "add", str(folder), "--ledger", str(ledger), "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["exit_code"] == 4
    assert cli.main(["history", "check", str(folder), "--ledger", str(ledger)]) == 4


def test_stopped_run_is_recorded_exits_3_and_is_kept_out_of_bands(tmp_path):
    """logical: a run stopped at its cost cap is recorded `stopped`, exit 3, and never enters
    another run's comparable history."""
    ledger = tmp_path / "runs.jsonl"
    folder = tmp_path / "capped"
    folder.mkdir()
    summary = evalkit_summary("capped")
    (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (folder / "run.json").write_text(
        json.dumps({"run_id": "capped", "stopped_at_cost_cap": True}), encoding="utf-8"
    )
    res = L.record_run(ledger, folder, at=at(1))
    assert res.exit_code == 3 and res.status == "stopped"
    assert _lines(ledger)[0]["status"] == "stopped"
    assert all(b.status == "stopped" for b in res.bands)
    for i in range(5):
        s = evalkit_summary(f"ok{i}")
        f = tmp_path / f"ok{i}"
        f.mkdir()
        (f / "summary.json").write_text(json.dumps(s), encoding="utf-8")
        res = L.record_run(ledger, f, at=at(2 + i))
    # the stopped run is the first entry: five complete runs later, histories hold 4 runs, not 5
    assert {b.n_history for b in res.bands} == {4}
    assert cli.main(["history", "check", str(folder), "--ledger", str(ledger)]) == 3


@pytest.mark.contract
def test_run_line_has_the_ledger_1_shape(tmp_path):
    """contract: the `run` entry carries exactly the spec §1 fields."""
    ledger = tmp_path / "runs.jsonl"
    run = fraud_run(tmp_path, 0, 1, 212, 4.1)
    entry = L.record_run(ledger, run, note="baseline", at=at(1)).entry
    assert entry is not None
    assert set(entry) == {
        "kind",
        "schema",
        "at",
        "project",
        "run_id",
        "source",
        "summary_sha256",
        "config_hash",
        "code_version",
        "snapshot",
        "evalkit",
        "recorded_with",
        "settings_sha256",
        "simulated",
        "status",
        "price_dates",
        "metrics",
        "heldout_rows_folded",
        "note",
        "prev",
    }
    assert entry["kind"] == "run" and entry["schema"] == L.LEDGER_SCHEMA == "ledger-1"
    assert entry["at"] == "2026-11-01T00:00:00Z" and entry["project"] == FRAUD
    assert entry["source"] == "metrics-1" and entry["status"] == "complete"
    assert entry["config_hash"] == "cfg-1" and entry["code_version"] == "c00"
    import hashlib

    assert entry["summary_sha256"] == hashlib.sha256(run.read_bytes()).hexdigest()
    assert [m["name"] for m in entry["metrics"]] == [m["name"] for m in fraud_rows(1, 212, 4.1)]
    # one project per ledger
    other = write_run(tmp_path / "other", [rate("x", 1, 2)], project="someone-else")
    with pytest.raises(L.LedgerError, match="one ledger holds one project"):
        L.record_run(ledger, other)
    with pytest.raises(L.LedgerError, match="at most 500"):
        L.record_run(ledger, fraud_run(tmp_path, 1, 1, 212, 4.1), note="x" * 501)


@pytest.mark.contract
def test_history_cli_flags_and_exit_codes(tmp_path, capsys):
    """contract: `evalkit history add|check|show|reset|verify` with --json print one object and
    exit 0/1/2/3/4 as documented."""
    from evalkit.ledger.cli import LEDGER_EXIT_CODES

    for code in ("0", "1", "2", "3", "4"):
        assert f"  {code}  " in LEDGER_EXIT_CODES
    ledger = tmp_path / "runs.jsonl"
    for i, r in enumerate(FRAUD_HISTORY):
        assert (
            cli.main(
                [
                    "history",
                    "add",
                    str(fraud_run(tmp_path, i, *r)),
                    "--ledger",
                    str(ledger),
                    "--json",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["recorded"] is True
    jump = fraud_run(tmp_path, 9, 14, 210, 4.15)
    assert cli.main(["history", "check", str(jump), "--ledger", str(ledger), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["recorded"] is False and out["alarms"] >= 1
    assert cli.main(["history", "show", "--ledger", str(ledger), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["runs"] == 6 and {s["metric"] for s in shown["series"]} == {
        "leakage",
        "auto_clear_rate",
        "cost_per_1000_usd",
        "latency_p95_stage2_ms",
    }
    assert (
        cli.main(
            [
                "history",
                "reset",
                "--metric",
                "leakage",
                "--reason",
                "new model",
                "--ledger",
                str(ledger),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["history", "verify", str(ledger), "--chain-only", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ledgers"][str(ledger)] == []
    assert cli.main(["history", "show", "--ledger", str(tmp_path / "nope" / "x.jsonl")]) == 0
    assert (
        cli.main(["history", "add", str(tmp_path / "missing.json"), "--ledger", str(ledger)]) == 2
    )


@pytest.mark.golden
def test_three_run_ledger_matches_the_golden_bytes(tmp_path):
    """golden: the ledger bytes for a fixture of three runs (one with a held-out split)."""
    ledger = tmp_path / "runs.jsonl"
    for i, r in enumerate(FRAUD_HISTORY[:3]):
        rows = [
            *fraud_rows(*r),
            rate("acc", 40 + i, 60, split="heldout", model="m1"),
            rate("acc", 10 + i, 20, split="heldout", model="m1", topic="t"),
        ]
        run = write_run(
            tmp_path / f"g{i}",
            rows,
            run_id=f"g-{i}",
            created_at=at(i + 1),
            provenance={"code_version": f"c{i}", "config_hash": "cfg"},
        )
        L.record_run(ledger, run, at=at(i + 1))
    golden = LEDGER_FIX / "runs-golden.jsonl"
    if UPDATE:
        golden.write_bytes(ledger.read_bytes())
    assert ledger.read_bytes() == golden.read_bytes()


def test_same_runs_in_a_fresh_ledger_give_identical_bytes_and_verify(tmp_path):
    """repro: adding the same runs to a fresh ledger gives identical bytes; verify passes."""
    runs = [fraud_run(tmp_path, i, *r) for i, r in enumerate(FRAUD_HISTORY)]
    out = []
    for name in ("a.jsonl", "b.jsonl"):
        ledger = tmp_path / name
        for i, run in enumerate(runs):
            L.record_run(ledger, run, at=at(i + 1))
        out.append(ledger.read_bytes())
        assert L.verify_ledger(ledger) == []
    assert out[0] == out[1]


def test_history_verify_fails_after_an_edited_middle_line_or_a_restored_rewrite(tmp_path):
    """repro: `history verify` passes on an append-only ledger in git, and fails after an edited
    middle line, and after a rewrite that was committed and then restored."""
    root = repo(tmp_path)
    ledger = root / "docs" / "measurements" / "runs.jsonl"
    for i, r in enumerate(FRAUD_HISTORY[:3]):
        L.record_run(ledger, fraud_run(tmp_path, i, *r), at=at(i + 1))
    base = commit(root, "three runs")
    assert cli.main(["history", "verify", str(ledger), "--rev", base]) == 0
    good = ledger.read_text(encoding="utf-8")
    lines = good.splitlines(keepends=True)
    ledger.write_text(lines[0] + lines[1].replace('"c01"', '"c99"') + lines[2], encoding="utf-8")
    assert any("does not follow" in p for p in L.verify_ledger(ledger))
    assert cli.main(["history", "verify", str(ledger), "--chain-only"]) == 1
    bad = commit(root, "rewrite")
    ledger.write_text(good, encoding="utf-8")
    L.record_run(ledger, fraud_run(tmp_path, 3, *FRAUD_HISTORY[3]), at=at(4))
    commit(root, "restore and append")
    assert L.verify_ledger(ledger) == []
    problems = L.verify_ledger(ledger, [base])
    assert any(bad[:12] in p for p in problems)
    assert cli.main(["history", "verify", str(ledger), "--rev", base]) == 1


@pytest.mark.safety
def test_heldout_rows_are_folded_and_a_planted_id_is_refused(tmp_path, capsys):
    """safety: held-out rows are stored only at split level; a held-out case id planted in a
    note is refused with --manifest (exit 2, nothing written); no case id in any output."""
    ledger = tmp_path / "runs.jsonl"
    rows = [
        rate("acc", 30, 40, split="heldout", model="m1"),
        rate("acc", 9, 10, split="heldout", model="m1", category="c-1"),
        rate("acc", 1, 1, split="secret", model="m1", item_hash="x"),
        rate("acc", 50, 60, split="public", model="m1"),
    ]
    run = write_run(tmp_path / "h", rows, run_id="h1")
    gates = tmp_path / "gates.yaml"
    gates.write_text(
        "gates:\n  - {metric: acc, op: '>=', value: 0.5}\nheldout_splits: [secret]\n",
        encoding="utf-8",
    )
    planted = f"rerun of case {HELD_IDS[0]}"
    code = cli.main(
        [
            "history",
            "add",
            str(run),
            "--ledger",
            str(ledger),
            "--gates",
            str(gates),
            "--manifest",
            str(MANIFEST),
            "--note",
            planted,
        ]
    )
    assert code == 2 and not ledger.exists()
    assert HELD_IDS[0] not in capsys.readouterr().err
    res = L.record_run(ledger, run, gates=gates, manifest=MANIFEST, note="clean")
    stored = res.entry["metrics"] if res.entry else []
    assert [m["labels"] for m in stored] == [
        {"split": "heldout", "model": "m1"},
        {"split": "public", "model": "m1"},
    ]
    assert res.entry and res.entry["heldout_rows_folded"] == 2
    text = ledger.read_text(encoding="utf-8")
    for split in ("heldout",):
        assert (
            H.ids_in_text(text, json.loads(MANIFEST.read_text(encoding="utf-8")), split=split) == []
        )
    assert "c-1" not in text and "item_hash" not in text


# ==================================== LED-02 alarm bands ====================================


def _independent(history: list[float], ses: list[float], new: float, new_se: float):
    """A second, separately written band: median, 1.4826 x MAD, floors, k = 2 and 3."""
    xs = sorted(history)
    n = len(xs)
    med = (xs[n // 2] + xs[(n - 1) // 2]) / 2
    dev = sorted(abs(x - med) for x in xs)
    mad = (dev[n // 2] + dev[(n - 1) // 2]) / 2
    se_sorted = sorted(ses)
    med_se = (se_sorted[n // 2] + se_sorted[(n - 1) // 2]) / 2
    s = max(1.4826 * mad, med_se, new_se)
    d = abs(new - med)
    return med, s, "alarm" if d > 3 * s else "warn" if d > 2 * s else "ok"


def test_bands_match_an_independent_median_mad_computation():
    """logical: centre, spread and status equal an independent computation on random histories,
    intervals included (SE = width / 3.92)."""
    rng = random.Random(5)
    for _ in range(300):
        n = rng.randint(5, 20)
        hist = []
        for _ in range(n):
            v = rng.gauss(10, 1)
            w = rng.choice([0.0, rng.random()])
            hist.append(
                {
                    "name": "x",
                    "unit": "ratio",
                    "value": v,
                    **({"ci_low": v - w, "ci_high": v + w, "ci_method": "m"} if w else {}),
                }
            )
        v = rng.gauss(10, 3)
        row = {"name": "x", "unit": "ratio", "value": v}
        b = L.band(hist, row)
        ses = [(h.get("ci_high", h["value"]) - h.get("ci_low", h["value"])) / 3.92 for h in hist]
        med, s, status = _independent([h["value"] for h in hist], ses, v, 0.0)
        assert b.centre == pytest.approx(med, abs=1e-12)
        assert b.spread == pytest.approx(s, abs=1e-12)
        assert b.status == status
        assert b.centre == statistics.median(h["value"] for h in hist)


def test_four_runs_are_learning_and_never_flag(tmp_path):
    """logical: fewer than 5 comparable runs is `learning`, however far the value moves."""
    ledger = tmp_path / "runs.jsonl"
    _history(tmp_path, ledger, FRAUD_HISTORY[:4])
    res = L.check_run(ledger, fraud_run(tmp_path, 8, 200, 10, 99.0))
    assert {b.status for b in res.bands} == {"learning"} and res.exit_code == 0
    assert all(b.centre is None for b in res.bands)


def test_warn_beyond_two_and_alarm_beyond_three_spreads():
    """logical: |x − c| > 2ŝ warns, > 3ŝ alarms; exactly 2ŝ is ok."""
    hist = [{"name": "x", "unit": "ratio", "value": v} for v in (9.0, 10.0, 10.0, 10.0, 11.0)]
    s = 1.4826  # MAD = 0 ... no: median |dev| over (1,0,0,0,1) is 0
    b0 = L.band(hist, {"name": "x", "unit": "ratio", "value": 10.0})
    assert b0.spread == 0 and b0.status == "degenerate"
    hist = [{"name": "x", "unit": "ratio", "value": v} for v in (8.0, 9.0, 10.0, 11.0, 12.0)]
    s = 1.4826 * 1.0
    cases = {
        10 + 2 * s: "ok",
        10 + 2 * s + 1e-9: "warn",
        10 + 3 * s + 1e-9: "alarm",
        10 - 3 * s - 1e-9: "alarm",
        10 - 2.5 * s: "warn",
    }
    for value, want in cases.items():
        assert L.band(hist, {"name": "x", "unit": "ratio", "value": value}).status == want
    flat = [{"name": "x", "unit": "ratio", "value": 1.0}] * 5
    b = L.band(flat, {"name": "x", "unit": "ratio", "value": 1.5})
    assert b.status == "warn" and b.spread == 0  # degenerate band: warns, never alarms


def test_gate_direction_alarms_only_on_the_failing_side(tmp_path):
    """logical: a `>=` gate alarms only below the band, a `<=` gate only above; the other side
    is `improved`."""
    hist = [{"name": "x", "unit": "ratio", "value": v} for v in (8.0, 9.0, 10.0, 11.0, 12.0)]
    up, down = (
        {"name": "x", "unit": "ratio", "value": 20.0},
        {"name": "x", "unit": "ratio", "value": 0.0},
    )
    assert L.band(hist, up, side="low").status == "improved"
    assert L.band(hist, down, side="low").status == "alarm"
    assert L.band(hist, up, side="high").status == "alarm"
    assert L.band(hist, down, side="high").status == "improved"
    assert L.band(hist, up).status == L.band(hist, down).status == "alarm"
    ledger = tmp_path / "runs.jsonl"
    _history(tmp_path, ledger, FRAUD_HISTORY)
    gates = parse_gates(
        "gates:\n  - {metric: leakage, op: '<=', value: 0.01}\n"
        "  - {metric: auto_clear_rate, op: '>=', value: 0.4}\n"
    )
    better = fraud_run(tmp_path, 20, 1, 300, 4.15)  # auto-clear jumps up: good for a >= gate
    res = L.check_run(ledger, better, gates=gates)
    by = {b.metric: b for b in res.bands}
    assert by["auto_clear_rate"].status == "improved" and by["auto_clear_rate"].side == "low"
    assert by["leakage"].side == "high"
    assert res.exit_code == 0
    assert L.check_run(ledger, better).exit_code == 1  # ungated: both sides alarm


def test_sampling_floor_stops_a_noisy_run_alarming():
    """logical: a tight history would alarm, but the new run's own SE is the floor of ŝ."""
    hist = [{"name": "x", "unit": "ratio", "value": v} for v in (10.0, 10.01, 9.99, 10.0, 10.02)]
    noisy = {
        "name": "x",
        "unit": "ratio",
        "value": 10.5,
        "ci_low": 9.0,
        "ci_high": 12.0,
        "ci_method": "bootstrap",
    }
    tight = {"name": "x", "unit": "ratio", "value": 10.5}
    assert L.band(hist, tight).status == "alarm"
    b = L.band(hist, noisy)
    assert b.status == "ok" and b.spread == pytest.approx(3.0 / 3.92) and b.run_se == b.spread
    # a rate with n and no interval: sqrt(p(1-p)/n), p clamped to [0.5/n, 1 - 0.5/n]
    from evalkit.ledger.runs import row_se

    assert row_se({"unit": "rate", "value": 0.0, "n": 50}) == pytest.approx(
        ((0.01 * 0.99) / 50) ** 0.5
    )


def test_band_reset_restarts_the_history(tmp_path):
    """logical: after a band_reset only later runs count; other metrics keep their history."""
    ledger = tmp_path / "runs.jsonl"
    _history(tmp_path, ledger, FRAUD_HISTORY)
    L.reset_band(ledger, metric="cost_per_1000_usd", reason="new model, new prices", at=at(10))
    res = L.check_run(ledger, fraud_run(tmp_path, 9, 1, 210, 9.0))
    by = {b.metric: b for b in res.bands}
    assert by["cost_per_1000_usd"].status == "learning" and by["cost_per_1000_usd"].n_history == 0
    assert by["leakage"].n_history == 6
    with pytest.raises(L.LedgerError):
        L.reset_band(ledger, metric="Bad Name", reason="x")
    with pytest.raises(L.LedgerError):
        L.reset_band(tmp_path / "empty.jsonl", metric="x", reason="no project")


@pytest.mark.contract
def test_band_json_fields_and_statuses(tmp_path, capsys):
    """contract: each band row's JSON fields; statuses come from one fixed set."""
    ledger = tmp_path / "runs.jsonl"
    _history(tmp_path, ledger, FRAUD_HISTORY)
    assert (
        cli.main(
            [
                "history",
                "check",
                str(fraud_run(tmp_path, 9, 20, 210, 4.1)),
                "--ledger",
                str(ledger),
                "--json",
            ]
        )
        == 1
    )
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {
        "schema",
        "project",
        "run_id",
        "status",
        "recorded",
        "summary_sha256",
        "exit_code",
        "alarms",
        "warns",
        "learning",
        "heldout_rows_folded",
        "bands",
        "errors",
    }
    for b in out["bands"]:
        assert set(b) >= {
            "metric",
            "labels",
            "unit",
            "value",
            "ci_low",
            "ci_high",
            "status",
            "n_history",
            "centre",
            "spread",
            "mad_spread",
            "history_se",
            "run_se",
            "warn_band",
            "alarm_band",
            "side",
            "z",
            "hints",
        }
        assert b["status"] in {
            "learning",
            "ok",
            "warn",
            "alarm",
            "improved",
            "degenerate",
            "stopped",
        }
        if b["spread"]:
            assert b["warn_band"] == pytest.approx(
                [b["centre"] - 2 * b["spread"], b["centre"] + 2 * b["spread"]]
            )


@pytest.mark.golden
def test_fraud_review_bedrock_history_leakage_jump_alarms_cost_drift_does_not(tmp_path):
    """golden: fraud-review-bedrock-shaped history (synthetic, its runs.jsonl figures): a
    leakage jump alarms; a cost drift within the run-to-run band does not."""
    ledger = tmp_path / "runs.jsonl"
    _history(tmp_path, ledger, FRAUD_HISTORY)
    gates = parse_gates("gates:\n  - {metric: leakage, op: '<=', value: 0.01, bound: upper}\n")
    jump = L.check_run(ledger, fraud_run(tmp_path, 10, 12, 209, 4.16), gates=gates)
    by = {b.metric: b for b in jump.bands}
    assert by["leakage"].status == "alarm" and jump.exit_code == 1
    assert by["cost_per_1000_usd"].status == "ok"
    drift = L.check_run(ledger, fraud_run(tmp_path, 11, 1, 211, 4.26), gates=gates)
    by = {b.metric: b for b in drift.bands}
    assert by["cost_per_1000_usd"].status == "ok" and drift.exit_code == 0
    assert by["cost_per_1000_usd"].centre == pytest.approx(4.15)


def test_same_ledger_gives_the_same_bands_and_exit_code(tmp_path, capsys):
    """repro: the same ledger and run give the same bands and exit code, byte for byte."""
    ledger = tmp_path / "runs.jsonl"
    _history(tmp_path, ledger, FRAUD_HISTORY)
    run = fraud_run(tmp_path, 12, 9, 205, 4.4)
    outs = []
    for _ in range(2):
        code = cli.main(["history", "check", str(run), "--ledger", str(ledger), "--json"])
        outs.append((code, capsys.readouterr().out))
    assert outs[0] == outs[1] and outs[0][0] == 1
    assert L.bands(ledger) == L.bands(ledger)


@pytest.mark.safety
def test_heldout_band_rows_only_at_split_level(tmp_path):
    """safety: bands for held-out rows exist only at split level (labels within split, model,
    judge), whatever the run carried."""
    ledger = tmp_path / "runs.jsonl"
    for i in range(6):
        rows = [
            rate("acc", 30 + i % 2, 40, split="heldout", model="m1"),
            rate("acc", 5, 10, split="heldout", model="m1", case_group="g7"),
        ]
        L.record_run(ledger, write_run(tmp_path / f"s{i}", rows, run_id=f"s{i}"), at=at(i + 1))
    shown = L.history(ledger)
    assert [dict(s.labels) for s in shown.series] == [{"split": "heldout", "model": "m1"}]
    assert shown.series[0].latest.heldout
    text = ledger.read_text(encoding="utf-8") + json.dumps(shown.as_dict())
    assert "case_group" not in text and "g7" not in text
