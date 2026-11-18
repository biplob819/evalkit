"""B5.7 drift and calibration monitor (LED-04; PSI levels of LED-05), five proof dimensions.

fraud-review-bedrock's `psi` (its src/fraudreview/monitor.py at ca8e895d200e, MON-03) is copied
below as the reference implementation, so its numbers must reproduce on a synthetic batch built
to its shape (its seven agent-visible numeric features). respectful-offers' churn block is its
vendored metrics-1 export (tests/fixtures/gates/respectful_offers) with synthetic
`drift.feature_share` rows. No network, no keys, no model call.
Regenerate the golden files after an intended change with EVALKIT_UPDATE_GOLDEN=1.
"""

from __future__ import annotations

import json
import math
import os
import random
from pathlib import Path
from typing import Any

import pytest

from evalkit import cli
from evalkit import drift as D
from evalkit.metrics import parse_metrics
from evalkit.stats import clopper_pearson
from tests.ledger_helpers import FIX, HELD_IDS, LEDGER_FIX, MANIFEST, no_network  # noqa: F401

UPDATE = os.environ.get("EVALKIT_UPDATE_GOLDEN") == "1"
RO_METRICS = FIX / "gates" / "respectful_offers" / "metrics.json"
FRAUD_FEATURES = (
    "amount",
    "risk_score",
    "ts_hour",
    "ts_dow",
    "amt_vs_usual",
    "category_freq",
    "card_history_n",
)


@pytest.fixture(autouse=True)
def _offline(no_network):  # noqa: F811
    yield


# ---- fraud-review-bedrock's rule, verbatim (numpy) -----------------------------------------------


def _fraud_psi(reference, current, bins: int = 10) -> float:
    np = pytest.importorskip("numpy")

    def _clean(values):
        arr = np.array([v for v in values if v is not None], dtype=float)
        return arr[np.isfinite(arr)]

    if bins < 2:
        raise ValueError("psi needs at least 2 bins")
    ref, cur = _clean(reference), _clean(current)
    if ref.size == 0 or cur.size == 0:
        raise ValueError("psi needs values in both samples")
    if ref.min() == ref.max():
        v = float(ref[0])
        edges = np.array([v, np.nextafter(v, math.inf)])
    else:
        edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1))[1:-1])
    k = edges.size + 1
    r = np.bincount(np.searchsorted(edges, ref, side="right"), minlength=k) / ref.size
    c = np.bincount(np.searchsorted(edges, cur, side="right"), minlength=k) / cur.size
    r, c = np.maximum(r, 1e-4), np.maximum(c, 1e-4)
    return float(np.sum((c - r) * np.log(c / r)))


def _fraud_level(value: float) -> str:
    if value < 0.1:
        return "ok"
    return "warn" if value <= 0.25 else "alarm"


def _fraud_batch(seed: int, n: int, shift: float) -> list[dict[str, Any]]:
    """Alerts shaped like fraud-review-bedrock's AlertCase features (synthetic values)."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        rows.append(
            {
                "amount": round(math.exp(rng.gauss(3.5 + shift, 1.1)), 2),
                "risk_score": round(min(1.0, max(0.0, rng.betavariate(2, 5 + 3 * shift))), 3),
                "ts_hour": rng.randint(0, 23),
                "ts_dow": rng.randint(0, 6),
                "amt_vs_usual": round(rng.lognormvariate(0, 0.6 + shift), 3),
                "category_freq": rng.choice([0.01, 0.05, 0.05, 0.1, 0.2, 0.2, 0.2, 0.4]),
                "card_history_n": None if rng.random() < 0.03 else rng.randint(0, 40),
            }
        )
    return rows


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# ==================================== logical ====================================


def test_psi_equals_fraud_review_bedrocks_psi_bit_for_bit():
    """logical: PSI equals fraud-review-bedrock's numpy `psi` exactly on random samples with
    ties, repeated edges, values on the edges, nulls and non-finite values."""
    rng = random.Random(11)
    for _ in range(1500):
        pool = [rng.choice([0, 1, 2, 2.5, 3]) for _ in range(4)]
        ref = [
            rng.choice(pool) if rng.random() < 0.4 else round(rng.gauss(0, 2), rng.randint(0, 3))
            for _ in range(rng.randint(1, 80))
        ]
        cur = [
            rng.choice(pool) if rng.random() < 0.4 else round(rng.gauss(0.4, 2), rng.randint(0, 3))
            for _ in range(rng.randint(1, 80))
        ]
        if rng.random() < 0.2:
            cur += [None, float("nan"), float("inf")]
        assert D.psi(ref, cur) == _fraud_psi(ref, cur)


def test_psi_rule_edges_open_ends_constant_reference_nulls_and_floor():
    """logical: the rule without numpy: decile edges with bins [e_i, e_{i+1}) (a value on an
    edge goes up), open outer bins, a constant reference's 3 bins, nulls dropped, 1e-4 floor."""
    ref = list(range(1, 11))  # deciles 1.9, 2.8, ... 9.1 (linear interpolation)
    edges = D.numeric_edges(ref)
    assert edges == pytest.approx([1.9, 2.8, 3.7, 4.6, 5.5, 6.4, 7.3, 8.2, 9.1])
    assert D.psi(ref, ref) == 0.0
    assert D.psi([*ref, None, float("nan")], ref) == 0.0
    # a constant reference: below, equal, above
    assert D.numeric_edges([5.0, 5.0]) == [5.0, math.nextafter(5.0, math.inf)]
    above = D.psi([5.0] * 10, [6.0] * 10)
    assert above == pytest.approx((1 - 1e-4) * math.log(1 / 1e-4) * 2)
    # a 5% share in a bin the reference left empty adds about 0.31 by itself
    one = D.psi_from_shares([0.5, 0.5, 0.0], [0.475, 0.475, 0.05])
    assert one == pytest.approx(
        2 * (0.475 - 0.5) * math.log(0.475 / 0.5) + (0.05 - 1e-4) * math.log(0.05 / 1e-4)
    )
    assert 0.30 < (0.05 - 1e-4) * math.log(0.05 / 1e-4) < 0.32
    with pytest.raises(D.DriftError):
        D.psi([None, float("nan")], [1.0])


def test_prebinned_shares_give_the_same_psi_as_rows_binned_the_same_way(tmp_path):
    """logical: shares handed over as metrics-1 `drift.feature_share` rows (respectful-offers'
    shape) give the PSI of the rows binned on the same edges."""
    ref_rows, new_rows = _fraud_batch(1, 400, 0.0), _fraud_batch(2, 300, 0.4)
    ref = D.freeze_reference(
        _jsonl(tmp_path / "r.jsonl", ref_rows),
        features=["amount"],
        created_at="2026-11-17T00:00:00Z",
    )
    prof = ref["features"]["amount"]
    report = D.check(ref, _jsonl(tmp_path / "n.jsonl", new_rows))
    from_rows = report.features[0]["psi"]
    new_shares = [0] * (len(prof["edges"]) + 1)
    import bisect

    for r in new_rows:
        new_shares[bisect.bisect_right(prof["edges"], r["amount"])] += 1
    new_shares = [c / len(new_rows) for c in new_shares]

    def doc(shares, run_id):
        rows = [
            {
                "name": "drift.feature_share",
                "unit": "rate",
                "value": s,
                "n": 300,
                "labels": {"feature": "amount", "bin": i},
            }
            for i, s in enumerate(shares)
        ]
        return {
            "schema_version": "metrics-1",
            "project": "respectful-offers",
            "run_id": run_id,
            "created_at": "2026-11-17T00:00:00Z",
            "metrics": rows,
        }

    (tmp_path / "ref.json").write_text(json.dumps(doc(prof["shares"], "ref")), encoding="utf-8")
    (tmp_path / "new.json").write_text(json.dumps(doc(new_shares, "new")), encoding="utf-8")
    binned_ref = D.freeze_reference(tmp_path / "ref.json")
    binned = D.check(binned_ref, tmp_path / "new.json")
    assert binned.features[0]["kind"] == "binned"
    assert binned.features[0]["psi"] == from_rows


def _rows(n: int, seed: int, bias: float = 0.0) -> tuple[list[float], list[int]]:
    rng = random.Random(seed)
    ps, ys = [], []
    for _ in range(n):
        p = rng.random()
        ps.append(round(p, 4))
        ys.append(1 if rng.random() < min(1.0, max(0.0, p + bias)) else 0)
    return ps, ys


def test_calibration_figures_match_direct_computation_and_an_independent_bootstrap():
    """logical: Brier, ECE and the 10 equal-width bins equal a direct computation; the intervals
    agree with a separately written bootstrap (another seed) to within its own noise."""
    ps, ys = _rows(600, 3)
    cal = D.calibration(ps, ys, seed=7, resamples=2000)
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ps)
    bins: dict[int, list[tuple[float, int]]] = {}
    for p, y in zip(ps, ys, strict=True):
        bins.setdefault(min(9, math.floor(p * 10 + 1e-12)), []).append((p, y))
    ece = sum(
        len(v) / len(ps) * abs(sum(y for _, y in v) / len(v) - sum(p for p, _ in v) / len(v))
        for v in bins.values()
    )
    assert cal["brier"]["value"] == pytest.approx(brier, abs=1e-12)
    assert cal["ece"]["value"] == pytest.approx(ece, abs=1e-12)
    for b in cal["bins"]:
        rows = bins.get(b["bin"], [])
        assert b["n"] == len(rows)
        if rows:
            iv = clopper_pearson(b["k"], b["n"])
            assert (b["obs_low"], b["obs_high"]) == (iv.lo, iv.hi)
    rng = random.Random(99)
    boot = []
    for _ in range(2000):
        idx = [rng.randrange(len(ps)) for _ in range(len(ps))]
        boot.append(sum((ps[i] - ys[i]) ** 2 for i in idx) / len(idx))
    boot.sort()
    lo, hi = boot[int(0.025 * 1999)], boot[int(0.975 * 1999)]
    assert cal["brier"]["ci_low"] == pytest.approx(lo, abs=0.004)
    assert cal["brier"]["ci_high"] == pytest.approx(hi, abs=0.004)
    assert cal["ece"]["ci_low"] <= cal["ece"]["value"] <= cal["ece"]["ci_high"]


def test_deltas_mover_holm_and_the_warn_and_alarm_tolerances():
    """logical: Brier by MOVER, bins by MOVER of Clopper-Pearson; a delta is changed after Holm,
    warn when changed for the worse, alarm only when its whole interval clears the tolerance
    (D-73: Brier 0.01, ECE 0.02, bin 0.10)."""
    ref = D.calibration(*_rows(3000, 1), seed=7, resamples=300)
    worse = D.calibration(*_rows(3000, 2, bias=0.18), seed=7, resamples=300)
    same = D.calibration(*_rows(3000, 4), seed=7, resamples=300)
    out = D.calibration_drift(ref, worse, seed=7, resamples=500)
    by = {d["name"]: d for d in out["deltas"]}
    b = by["brier"]
    d = worse["brier"]["value"] - ref["brier"]["value"]
    lo = d - math.sqrt(
        (worse["brier"]["value"] - worse["brier"]["ci_low"]) ** 2
        + (ref["brier"]["ci_high"] - ref["brier"]["value"]) ** 2
    )
    assert b["delta"] == pytest.approx(d) and b["ci_low"] == pytest.approx(min(lo, d))
    assert (
        b["changed"] and b["worse"] and b["status"] == ("alarm" if b["ci_low"] > 0.01 else "warn")
    )
    assert by["ece"]["status"] == "alarm"
    assert out["bins_status"] == "compared" and out["level"] == "alarm"
    bin5 = by["bin 5"]
    r5, n5 = ref["bins"][5], worse["bins"][5]
    ri, ni = clopper_pearson(r5["k"], r5["n"]), clopper_pearson(n5["k"], n5["n"])
    dd = n5["k"] / n5["n"] - r5["k"] / r5["n"]
    assert bin5["ci_high"] == pytest.approx(
        dd + math.sqrt((ni.hi - n5["k"] / n5["n"]) ** 2 + (r5["k"] / r5["n"] - ri.lo) ** 2)
    )
    calm = D.calibration_drift(ref, same, seed=7, resamples=500)
    assert calm["level"] == "ok" and all(x["status"] in ("ok", "changed") for x in calm["deltas"])
    ps = [x["p_value"] for x in calm["deltas"]]
    assert all(h >= p for h, p in zip([x["p_holm"] for x in calm["deltas"]], ps, strict=True))
    loose = D.calibration_drift(
        ref, worse, tolerances={"brier": 1.0, "ece": 1.0, "bin": 1.0}, seed=7, resamples=500
    )
    assert loose["level"] == "warn"  # changed for the worse, inside the tolerance


def test_no_drift_seldom_flags_when_the_new_batch_is_smaller():
    """logical (verifier finding): a perfectly calibrated model with no drift. ECE is biased
    upward in small samples, so a smaller new batch has a larger ECE with nothing changed; the
    ECE delta is judged against what no drift gives at the two batch sizes, so across seeds and
    size ratios (reference >= new) the alarm rate stays at most 5% and the warn rate low."""
    checks = alarms = warns = ece_flags = 0
    for ref_n, ref_seed in ((2000, 11), (500, 12)):
        ref = D.calibration(*_rows(ref_n, ref_seed), seed=7, resamples=100)
        for n in (10, 30, 100, 300, ref_n):
            for s in range(12):
                new = D.calibration(*_rows(n, 1000 * ref_seed + 37 * n + s), resamples=100)
                out = D.calibration_drift(ref, new, resamples=400)
                by = {d["name"]: d for d in out["deltas"]}
                checks += 1
                alarms += out["level"] == "alarm"
                warns += out["level"] == "warn"
                ece_flags += by["ece"]["status"] in ("warn", "alarm")
    assert checks == 120
    assert alarms / checks <= 0.05, (alarms, checks)
    assert ece_flags / checks <= 0.05, (ece_flags, checks)
    assert warns / checks <= 0.10, (warns, checks)


def test_true_calibration_drift_still_alarms_in_a_smaller_batch():
    """logical: the debiased ECE delta still alarms on real miscalibration in a batch smaller
    than the reference, and reports the delta net of the no-drift expectation."""
    ref = D.calibration(*_rows(2000, 21), seed=7, resamples=100)
    for n, bias in ((300, 0.2), (100, 0.3)):
        new = D.calibration(*_rows(n, 22 + n, bias=bias), resamples=100)
        out = D.calibration_drift(ref, new, resamples=1000)
        ece = {d["name"]: d for d in out["deltas"]}["ece"]
        assert out["level"] == "alarm" and ece["status"] == "alarm", (n, ece)
        raw = new["ece"]["value"] - ref["ece"]["value"]
        assert ece["delta_raw"] == pytest.approx(raw)
        assert ece["delta"] == pytest.approx(raw - ece["no_drift_expected"])
        assert ece["ci_low"] <= ece["delta"] <= ece["ci_high"]


def test_a_categorical_feature_is_profiled_even_when_not_listed_in_features(tmp_path, capsys):
    """contract (verifier finding): `drift reference --categorical X` profiles X by category
    whether or not --features names it; nothing named is dropped silently. A --categorical
    name that is not a column exits 2."""
    rows = _jsonl(
        tmp_path / "ref.jsonl",
        [{"amount": float(i), "channel": ["web", "app", "phone"][i % 3]} for i in range(60)],
    )
    out = tmp_path / "ref.json"
    argv = ["drift", "reference", "--data", str(rows), "--out", str(out)]
    assert cli.main([*argv, "--features", "amount", "--categorical", "channel"]) == 0
    feats = json.loads(out.read_text(encoding="utf-8"))["features"]
    assert sorted(feats) == ["amount", "channel"]
    assert feats["channel"]["kind"] == "categorical" and feats["amount"]["kind"] == "numeric"
    assert cli.main([*argv, "--categorical", "amount"]) == 0
    feats = json.loads(out.read_text(encoding="utf-8"))["features"]
    assert list(feats) == ["amount"] and feats["amount"]["kind"] == "categorical"
    capsys.readouterr()
    assert cli.main([*argv, "--features", "amount", "--categorical", "chanel"]) == 2
    assert "chanel" in capsys.readouterr().err


def test_quantile_bins_are_reported_not_aligned_and_never_compared(tmp_path):
    """logical: respectful-offers' quantile reliability bins: Brier and ECE are compared, per-bin
    drift is "not available" (D-73); declared equal-width bins are compared."""
    ref = D.freeze_reference(RO_METRICS, calibration_prefix="churn")
    new = json.loads(RO_METRICS.read_text(encoding="utf-8"))
    new["run_id"] = "next-period"
    path = tmp_path / "new.json"
    path.write_text(json.dumps(new), encoding="utf-8")
    report = D.check(ref, path, resamples=500)
    cal = report.calibration
    assert cal is not None
    assert cal["bins"] == "not available" and cal["bins_status"] == "bins_not_aligned"
    assert [d["name"] for d in cal["deltas"]] == ["brier", "ece"]
    declared = D.check(ref, path, config={"calibration": {"binning": "equal-width"}}, resamples=500)
    assert declared.calibration and declared.calibration["bins_status"] == "compared"
    assert len(declared.calibration["deltas"]) == 12


def test_psi_levels_at_the_boundaries_and_per_feature_overrides():
    """logical (LED-05): 0.0999 ok, 0.1 warn, 0.25 warn, 0.2501 alarm, unrounded; a drift
    config overrides the levels per feature."""
    assert [D.psi_level(x) for x in (0.0999, 0.1, 0.25, 0.2501)] == ["ok", "warn", "warn", "alarm"]
    assert D.psi_level(0.09999999999) == "ok" and D.psi_level(0.25000000001) == "alarm"
    cfg = D.load_config({"features": {"amount": {"psi_warn": 0.05, "psi_alarm": 0.08}}})
    assert cfg.levels("amount") == (0.05, 0.08) and cfg.levels("ts_hour") == (0.1, 0.25)
    for bad in (
        {"psi_warn": 0.3, "psi_alarm": 0.2},
        {"nope": 1},
        {"psi_warn": -1},
        {"calibration": {"binning": "weird"}},
    ):
        with pytest.raises(D.DriftError):
            D.load_config(bad)


# ==================================== contract ====================================


@pytest.mark.contract
def test_reference_and_report_shapes_and_an_edited_profile_exits_2(tmp_path, capsys):
    """contract: `drift-ref-1` and `drift-1` shapes; an edited profile is refused (exit 2)."""
    ref_path = _jsonl(tmp_path / "ref.jsonl", _fraud_batch(1, 300, 0.0))
    new_path = _jsonl(tmp_path / "new.jsonl", _fraud_batch(2, 200, 0.3))
    out = tmp_path / "ref.json"
    assert (
        cli.main(
            [
                "drift",
                "reference",
                "--data",
                str(ref_path),
                "--features",
                ",".join(FRAUD_FEATURES),
                "--out",
                str(out),
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    ref = json.loads(out.read_text(encoding="utf-8"))
    assert set(ref) == {
        "schema",
        "created_at",
        "source_sha256",
        "source_kind",
        "n_rows",
        "features",
        "profile_sha256",
    }
    assert ref["schema"] == "drift-ref-1"
    assert set(ref["features"]["amount"]) == {"kind", "edges", "shares", "n", "missing_share"}
    report_path = tmp_path / "drift.json"
    code = cli.main(
        [
            "drift",
            "check",
            "--reference",
            str(out),
            "--data",
            str(new_path),
            "--out",
            str(report_path),
            "--json",
        ]
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert json.loads(capsys.readouterr().out) == report and code == report["exit_code"]
    assert set(report) == {
        "schema",
        "exit_code",
        "verdict",
        "alarms",
        "warns",
        "reference",
        "new",
        "settings",
        "features",
        "calibration",
    }
    assert set(report["features"][0]) == {
        "feature",
        "kind",
        "psi",
        "psi_shown",
        "level",
        "psi_warn",
        "psi_alarm",
        "bins",
        "n_ref",
        "n_new",
        "expected_psi",
        "small_batch",
        "missing_share_ref",
        "missing_share_new",
    }
    edited = dict(ref)
    edited["features"] = {
        **ref["features"],
        "ts_dow": {**ref["features"]["ts_dow"], "shares": [1.0]},
    }
    out.write_text(json.dumps(edited), encoding="utf-8")
    assert cli.main(["drift", "check", "--reference", str(out), "--data", str(new_path)]) == 2
    assert "profile_sha256" in capsys.readouterr().err
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert (
        cli.main(["drift", "check", "--reference", str(tmp_path / "x"), "--data", str(empty)]) == 2
    )


@pytest.mark.contract
def test_emitted_metrics_validate_and_enter_the_history(tmp_path, monkeypatch):
    """contract: --emit-metrics writes a valid metrics-1 file that `history add` accepts, so the
    PSI panel and LED-02 bands see drift checks."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1795000000")
    ref_rows = _fraud_batch(1, 300, 0.0)
    for i, r in enumerate(ref_rows):
        r["p"], r["y"] = round(r["risk_score"], 3), int(i % 7 == 0)
    ref = D.freeze_reference(
        _jsonl(tmp_path / "r.jsonl", ref_rows),
        features=["amount"],
        prob="p",
        label="y",
        resamples=200,
    )
    (tmp_path / "ref.json").write_text(json.dumps(ref), encoding="utf-8")
    new_rows = [dict(r) for r in ref_rows[:150]]
    emitted = tmp_path / "drift-metrics.json"
    code = cli.main(
        [
            "drift",
            "check",
            "--reference",
            str(tmp_path / "ref.json"),
            "--data",
            str(_jsonl(tmp_path / "n.jsonl", new_rows)),
            "--emit-metrics",
            str(emitted),
            "--project",
            "fraud-review-bedrock",
            "--resamples",
            "200",
        ]
    )
    assert code in (0, 1)
    doc = parse_metrics(emitted.read_text(encoding="utf-8"))
    names = {m.name for m in doc.metrics}
    assert names == {
        "drift.psi",
        "drift.brier",
        "drift.ece",
        "drift.brier_delta",
        "drift.ece_delta",
    }
    assert doc.run_id.startswith("drift-")
    assert cli.main(["history", "add", str(emitted), "--ledger", str(tmp_path / "runs.jsonl")]) == 0


# ==================================== golden ====================================


def _ro_with_shares(moved: bool, run_id: str, brier: float | None = None) -> dict[str, Any]:
    data = json.loads(RO_METRICS.read_text(encoding="utf-8"))
    data["run_id"] = run_id
    base = {"tenure_band": [0.30, 0.25, 0.25, 0.20], "plan": [0.55, 0.30, 0.15]}
    shifts = {"tenure_band": 0.18, "plan": 0.25} if moved else {"tenure_band": 0.0, "plan": 0.0}
    for feature, shares in base.items():
        shift = shifts[feature]
        now = [
            max(0.0, s + (shift if i == 0 else -shift / (len(shares) - 1)))
            for i, s in enumerate(shares)
        ]
        for i, s in enumerate(now):
            data["metrics"].append(
                {
                    "name": "drift.feature_share",
                    "unit": "rate",
                    "value": round(s, 6),
                    "n": 96843,
                    "labels": {"feature": feature, "bin": i},
                }
            )
    if brier is not None:
        for m in data["metrics"]:
            if m["name"] == "churn.brier":
                m.update(value=brier, ci_low=brier - 0.0004, ci_high=brier + 0.0004)
    return data


@pytest.mark.golden
def test_respectful_offers_shaped_metrics_end_to_end(tmp_path, monkeypatch):
    """golden: respectful-offers' churn block and planned `drift.feature_share` rows, reference
    and next period through the CLI; the drift-1 output equals the committed golden."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1795000000")
    (tmp_path / "ref.json").write_text(
        json.dumps(_ro_with_shares(False, "kkbox-2026-10"), sort_keys=True), encoding="utf-8"
    )
    (tmp_path / "new.json").write_text(
        json.dumps(_ro_with_shares(True, "kkbox-2026-11", brier=0.0712), sort_keys=True),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "drift",
                "reference",
                "--metrics",
                str(tmp_path / "ref.json"),
                "--calibration-prefix",
                "churn",
                "--out",
                str(tmp_path / "profile.json"),
            ]
        )
        == 0
    )
    out = tmp_path / "drift.json"
    code = cli.main(
        [
            "drift",
            "check",
            "--reference",
            str(tmp_path / "profile.json"),
            "--metrics",
            str(tmp_path / "new.json"),
            "--out",
            str(out),
        ]
    )
    report = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1 and report["verdict"] == "alarm"
    levels = {f["feature"]: f["level"] for f in report["features"]}
    assert levels == {"plan": "alarm", "tenure_band": "warn"}
    by = {d["name"]: d for d in report["calibration"]["deltas"]}
    assert by["brier"]["status"] == "alarm" and report["calibration"]["bins"] == "not available"
    golden = LEDGER_FIX / "drift-respectful-offers.json"
    if UPDATE:
        golden.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    # floats compared to 10 significant digits: libm's log and sqrt may differ in the last bit
    # between operating systems; everything else must match exactly
    assert _rounded(json.loads(out.read_text(encoding="utf-8"))) == _rounded(
        json.loads(golden.read_text(encoding="utf-8"))
    )


def _rounded(obj: Any) -> Any:
    if isinstance(obj, float):
        return float(f"{obj:.10g}")
    if isinstance(obj, dict):
        return {k: _rounded(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rounded(v) for v in obj]
    return obj


@pytest.mark.golden
def test_fraud_review_bedrock_drift_reproduces_its_numbers(tmp_path):
    """golden: a synthetic batch built to fraud-review-bedrock's shape gives exactly its
    `drift()` output: per feature (PSI rounded to 4 decimals, level)."""
    ref_rows, new_rows = _fraud_batch(21, 500, 0.0), _fraud_batch(22, 250, 0.35)
    ref = D.freeze_reference(_jsonl(tmp_path / "r.jsonl", ref_rows), features=FRAUD_FEATURES)
    report = D.check(ref, _jsonl(tmp_path / "n.jsonl", new_rows))
    ours = {f["feature"]: (round(f["psi"], 4), f["level"]) for f in report.features}
    theirs = {}
    for name in FRAUD_FEATURES:
        value = _fraud_psi([r[name] for r in ref_rows], [r[name] for r in new_rows])
        theirs[name] = (round(value, 4), _fraud_level(value))
    assert ours == theirs
    assert {lvl for _, lvl in ours.values()} >= {"ok", "alarm"}
    assert all(f["psi_shown"] == f"{f['psi']:.4f}" for f in report.features)


# ==================================== repro ====================================


def test_seeded_intervals_and_reports_repeat_exactly(tmp_path):
    """repro: the same inputs and seed give the same intervals and the same drift-1 bytes."""
    ps, ys = _rows(400, 8)
    assert D.calibration(ps, ys, resamples=500) == D.calibration(ps, ys, resamples=500)
    assert D.calibration(ps, ys, seed=8, resamples=500) != D.calibration(ps, ys, resamples=500)
    rows = [{"x": p, "p": p, "y": y} for p, y in zip(ps, ys, strict=True)]
    ref = D.freeze_reference(
        _jsonl(tmp_path / "r.jsonl", rows),
        features=["x"],
        prob="p",
        label="y",
        resamples=300,
        created_at="2026-11-17T00:00:00Z",
    )
    new = _jsonl(tmp_path / "n.jsonl", rows[:200])
    a = json.dumps(D.check(ref, new, resamples=300).as_dict(), sort_keys=True)
    b = json.dumps(D.check(ref, new, resamples=300).as_dict(), sort_keys=True)
    assert a == b


# ==================================== safety ====================================


@pytest.mark.safety
def test_outputs_hold_aggregates_only(tmp_path):
    """safety: the reference and the report carry edges, category names, shares and aggregates,
    never a row: no other column, and no value that is not an edge."""
    rng = random.Random(4)
    rows = [
        {
            "secret_note": f"ROW-TEXT-{i}",
            "x": round(rng.gauss(0, 1), 7) + 1e-7 * i,
            "p": round(rng.random(), 6),
            "y": rng.randint(0, 1),
        }
        for i in range(200)
    ]
    ref = D.freeze_reference(
        _jsonl(tmp_path / "r.jsonl", rows), features=["x"], prob="p", label="y", resamples=200
    )
    report = D.check(ref, _jsonl(tmp_path / "n.jsonl", rows[::-1]), resamples=200).as_dict()
    text = json.dumps(ref) + json.dumps(report)
    assert "ROW-TEXT" not in text and "secret_note" not in text
    edges = set(ref["features"]["x"]["edges"])
    for r in rows:
        if r["x"] not in edges:
            assert repr(r["x"]) not in text


@pytest.mark.safety
def test_heldout_ids_in_a_reference_are_refused_by_ids_in_text(tmp_path, capsys):
    """safety: a categorical feature whose values are held-out case ids would leak them; with
    --manifest the reference is refused (exit 2) and nothing is written."""
    rows = [{"case": HELD_IDS[i % 3], "score": i} for i in range(30)]
    data = _jsonl(tmp_path / "r.jsonl", rows)
    out = tmp_path / "ref.json"
    code = cli.main(
        [
            "drift",
            "reference",
            "--data",
            str(data),
            "--features",
            "case",
            "--out",
            str(out),
            "--manifest",
            str(MANIFEST),
        ]
    )
    assert code == 2 and not out.exists()
    err = capsys.readouterr().err
    assert all(i not in err for i in HELD_IDS)
    assert (
        cli.main(
            [
                "drift",
                "reference",
                "--data",
                str(data),
                "--features",
                "score",
                "--out",
                str(out),
                "--manifest",
                str(MANIFEST),
            ]
        )
        == 0
    )
