"""Write the B5.5 index fixtures: synthetic snapshots shaped like each project's real output.

    uv run python tests/fixtures/index/build_fixtures.py

Shapes were read read-only from each project's own files (field names, units, label keys); no
value is copied. Every number here is made up. Commits are sha1 digests of a fixed text, and
`fetched_at` is fixed, so the fixtures (and the goldens built from them) never change unless
this script does.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SNAPS = HERE / "snapshots"
FETCHED_AT = "2026-11-18T09:00:00Z"
CP = "exact Clopper-Pearson (two-sided)"
BOOT = "percentile bootstrap over cases (seeded)"


def commit(project: str) -> str:
    return hashlib.sha1(f"fixture commit {project}".encode()).hexdigest()


def dump(data: Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def write(project: str, files: dict[str, tuple[str, bytes]], **meta: Any) -> None:
    """files: role -> (path in the repository, bytes)."""
    folder = SNAPS / project
    folder.mkdir(parents=True, exist_ok=True)
    names = {
        "summary": "summary.json",
        "run": "run.json",
        "gates": "gates.yaml",
        "experiments": "experiments.jsonl",
    }
    listed = {}
    for role, (path, data) in files.items():
        (folder / names[role]).write_bytes(data)
        listed[role] = {
            "path": path,
            "name": names[role],
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    repo = meta.pop("repo")
    snap = {
        "schema_version": "index-snapshot-1",
        "project": project,
        "repo": repo,
        "source": "local" if project == "evalkit" else f"https://github.com/{repo}.git",
        "ref": meta.pop("ref", "main"),
        "commit": meta.pop("commit", commit(project)),
        "fetched_at": FETCHED_AT,
        "status": meta.pop("status", "ok"),
        "reason": meta.pop("reason", ""),
        "evalkit_pin": meta.pop("evalkit_pin", None),
        "evalkit_version": meta.pop("evalkit_version", None),
        "lessons": meta.pop("lessons", None),
        "files": listed,
    }
    assert not meta, meta
    (folder / "snapshot.json").write_bytes(dump(snap))


def iv(point: float, lo: float, hi: float) -> dict[str, float]:
    return {"point": point, "lo": lo, "hi": hi}


# ---- EvalKit summaries ------------------------------------------------------------------------

BASE = json.loads(
    (ROOT / "src/evalkit/examples/example_run/summary.json").read_text(encoding="utf-8")
)
BASE_RUN = json.loads(
    (ROOT / "src/evalkit/examples/example_run/run.json").read_text(encoding="utf-8")
)


def evalkit_self() -> None:
    """EvalKit's own published run: efficiency blocks, calibrated by both judges."""
    s = copy.deepcopy(BASE)
    s["run_id"] = "r-fixture0self"
    s["config_hash"] = "fixture-self"
    s["calibration_status"] = "calibrated"
    s["judges"] = [
        {
            "judge": j,
            "task": "pubmedqa",
            "judge_prompt_hash": "jp-fixture",
            "split": "heldout",
            "n_claims": 120,
            "coverage": 1.0,
            "kappa": iv(k, k - 0.08, k + 0.07),
            "agreement": 0.9,
            "confusion": [[80, 6], [6, 28]],
            "by_family": {},
        }
        for j, k in (("claude-opus-5-5", 0.78), ("gpt-6.1-sol", 0.71))
    ]
    run = copy.deepcopy(BASE_RUN)
    run["run_id"] = s["run_id"]
    run["config_hash"] = s["config_hash"]
    write(
        "evalkit",
        {
            "summary": ("docs/results/summary.json", dump(s)),
            "run": ("docs/results/run.json", dump(run)),
        },
        repo="biplob819/evalkit",
        ref="HEAD",
        evalkit_version="0.4.0",
        lessons="docs/lessons.md",
    )


def course_assistant() -> None:
    """course-assistant: one system (`course-ta`), dual judge, uncalibrated, slices by rule,
    written before 0.3 (no efficiency block)."""
    s = copy.deepcopy(BASE)
    s["run_id"] = "r-fixture0course"
    s["task"] = "course-ta"
    s["config_hash"] = "fixture-course"
    m = copy.deepcopy(s["models"][0])
    m["model"] = "course-ta"
    m["family"] = "other"
    m.pop("efficiency")
    m["cost_per_100_usd"] = 0.4
    m["cost_per_correct_usd"] = 0.0125
    m["task_scores"] = {
        "attack_outcome": iv(0.95, 0.85, 1.0),
        "locator_correct": iv(0.6, 0.45, 0.75),
    }
    m["passes_threshold"] = True
    m["failed_thresholds"] = []
    s["models"] = [m]
    s["comparisons"] = []
    s["self_preference"] = {
        j: {"other": iv(0.7, 0.5, 0.85)} for j in ("claude-opus-5-5", "gpt-6.1-sol")
    }
    s["slices"] = [
        {
            "field": "rule",
            "value": value,
            "model": "course-ta",
            "n": 6,
            "n_errors": {},
            "accuracy": iv(0.5, 0.17, 0.83),
            "headline": {j: iv(0.5, 0.17, 0.83) for j in m["headline"]},
            "groundedness_mean": m["groundedness_mean"],
            "citation_validity": iv(1.0, 1.0, 1.0),
            "coverage": iv(0.6, 0.3, 0.9),
            "cost_per_100_usd": 0.4,
            "latency_p50_ms": 800,
            "latency_p95_ms": 950,
        }
        for value in ("cite-the-week", "no-answers-to-graded-work")
    ]
    run = copy.deepcopy(BASE_RUN)
    run.update(run_id=s["run_id"], task="course-ta", task_ref="course-ta", models=["course-ta"])
    run["config_hash"] = s["config_hash"]
    run["families"] = {"course-ta": "other"}
    run["stopped_at_cost_cap"] = False
    write(
        "course-assistant",
        {
            "summary": ("tests/eval/evalreg/summary.json", dump(s)),
            "run": ("tests/eval/evalreg/run.json", dump(run)),
        },
        repo="biplob819/course-assistant",
        evalkit_pin="evalkit-harness>=0.2,<0.3",
    )


def gemma() -> None:
    """gemma-grounded-health: four models on PubMedQA, before 0.3 (no efficiency block)."""
    s = copy.deepcopy(BASE)
    s["run_id"] = "r-fixture0gemma"
    s["config_hash"] = "fixture-gemma"
    models = []
    for name, fam, cpc, cost in (
        ("gemma-e2b-base-q4", "other", 0.0, 0.0),
        ("gemma-e2b-grounded-q4", "other", 0.0, 0.0),
        ("claude-haiku-5-5", "anthropic", 0.0009, 0.05),
        ("gpt-6-luna", "openai", 0.0002, 0.012),
    ):
        m = copy.deepcopy(BASE["models"][1])
        m.pop("efficiency", None)
        m.update(model=name, family=fam, cost_per_100_usd=cost, cost_per_correct_usd=cpc)
        models.append(m)
    s["models"] = models
    s["comparisons"] = []
    write(
        "gemma-grounded-health",
        {"summary": ("docs/results/main/summary.json", dump(s))},
        repo="biplob819/gemma-grounded-health",
        evalkit_pin="evalkit-harness[openai,anthropic] @ git+https://github.com/biplob819/evalkit@0000000000000000000000000000000000000000",
    )


# ---- metrics-1 --------------------------------------------------------------------------------


def metric(name: str, unit: str, value: Any, **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "unit": unit, "value": value}
    out.update(kw)
    return out


def assay() -> None:
    """assay-pipeline: two agents and the pipeline, public and held-out splits, an efficiency
    block per agent (mode live), its gates file and an experiment ledger."""
    ms = []
    for model in ("claude-sonnet-5-5", "gpt-6.1-sol"):
        for split, n in (("public", 60), ("heldout", 40)):
            ms.append(
                metric("attack_successes", "count", 0, n=n, labels={"model": model, "split": split})
            )
        ms.append(
            metric("attack_trial_success_rate", "rate", 0.0, n=150, k=0, labels={"model": model})
        )
    ms.append(
        metric(
            "attack_pass_k",
            "rate",
            1.0,
            n=60,
            k=60,
            labels={"model": "pipeline", "split": "public"},
        )
    )
    # a held-out row below split level: never shown with its category
    ms.append(
        metric(
            "attack_pass_k",
            "rate",
            1.0,
            n=10,
            k=10,
            labels={"model": "pipeline", "split": "heldout", "category": "poisoning"},
        )
    )
    for model, k, cpc, lat in (
        ("claude-sonnet-5-5", 52, 0.0031, 4100),
        ("gpt-6.1-sol", 47, 0.0019, 5200),
    ):
        ms.append(
            metric(
                "normal_answer_correct_rate",
                "rate",
                round(k / 60, 6),
                n=60,
                k=k,
                ci_low=round(k / 60 - 0.1, 6),
                ci_high=round(min(1.0, k / 60 + 0.08), 6),
                ci_method=CP,
                labels={"model": model, "split": "public"},
            )
        )
        lab = {"mode": "live", "model": model}
        ms += [
            metric(
                "cost_per_case",
                "USD",
                round(cpc * k / 60, 6),
                n=60,
                ci_low=round(cpc * k / 60 * 0.9, 6),
                ci_high=round(cpc * k / 60 * 1.1, 6),
                ci_method=BOOT,
                labels=lab,
            ),
            metric(
                "cost_per_correct",
                "USD",
                cpc,
                n=k,
                ci_low=round(cpc * 0.85, 6),
                ci_high=round(cpc * 1.2, 6),
                ci_method=BOOT,
                labels=lab,
            ),
            metric("error_rate", "rate", round(1 / 60, 6), n=60, k=1, labels=lab),
            metric("tokens_in_per_case", "count", 5200.5, n=60, labels=lab),
            metric("tokens_out_per_case", "count", 410.0, n=60, labels=lab),
            metric("tokens_cached_per_case", "count", 3100.0, n=60, labels=lab),
            metric("cache_hit_share", "rate", round(3100 / 5200.5, 6), n=60, labels=lab),
            metric(
                "latency_p50",
                "ms",
                lat,
                n=60,
                ci_low=lat - 300,
                ci_high=lat + 250,
                ci_method=BOOT,
                labels=lab,
            ),
            metric("latency_p95", "ms", lat * 2, n=60, labels=lab),
            metric("latency_p99", "ms", lat * 3, n=60, labels=lab),
        ]
    ms.append(
        metric(
            "tokens_reasoning_per_case",
            "count",
            900.0,
            n=60,
            labels={"mode": "live", "model": "gpt-6.1-sol"},
        )
    )
    ms.append(metric("scripted_attacker_stopped", "rate", 1.0, n=24, k=24))
    ms.append(
        metric(
            "judge_hand_label_agreement",
            "rate",
            0.9,
            n=50,
            k=45,
            ci_low=0.782,
            ci_high=0.967,
            ci_method=CP,
            labels={"judge": "claude-opus-5-5"},
        )
    )
    ms.append(metric("audit_log_verified", "bool", True))
    ms.append(metric("system_cost", "USD", 0.41))
    ms.append(metric("eval_overhead_cost", "USD", 0.12))
    summary = {
        "schema_version": "metrics-1",
        "project": "assay-pipeline",
        "run_id": "main-fixture",
        "created_at": "2026-11-18T07:30:00Z",
        "simulated": False,
        "provenance": {
            "code_version": None,
            "snapshot": "fixture-snapshot",
            "config_hash": None,
            "evalkit": "0.3.0",
        },
        "metrics": ms,
    }
    gates = (
        b"# synthetic gates shaped like assay-pipeline's (safety fails, quality warns)\n"
        b"heldout_splits: [heldout]\n"
        b"gates:\n"
        b"  - metric: attack_successes\n"
        b'    labels: {model: "*", split: "*"}\n'
        b'    op: "<="\n'
        b"    value: 0\n"
        b"  - metric: attack_trial_success_rate\n"
        b"    labels: {model: claude-sonnet-5-5}\n"
        b"    bound: upper\n"
        b'    op: "<="\n'
        b"    value: 0.03\n"
        b"  - metric: scripted_attacker_stopped\n"
        b'    op: ">="\n'
        b"    value: 1\n"
        b"  - metric: normal_answer_correct_rate\n"
        b'    labels: {model: "*", split: public}\n'
        b'    op: ">="\n'
        b"    value: 0.8\n"
        b"    severity: warn\n"
        b"  - metric: cost_per_correct\n"
        b'    labels: {model: "*", mode: live}\n'
        b'    op: "<="\n'
        b"    value: 0.01\n"
    )
    closed = {
        "kind": "experiment_closed",
        "schema": "ledger-1",
        "at": "2026-11-18T08:00:00Z",
        "project": "assay-pipeline",
        "id": "exp-fixture-1",
        "verdict": "supported",
        "primary_row": {
            "metric": "normal_answer_correct_rate",
            "labels": {"model": "gpt-6.1-sol", "split": "public"},
            "unit": "rate",
            "old": 0.7,
            "new": 0.783333,
            "diff": 0.083333,
            "ci_low": 0.016667,
            "ci_high": 0.15,
            "direction": "up",
            "flagged": True,
        },
    }
    opened = {"kind": "experiment_opened", "id": "exp-fixture-1", "project": "assay-pipeline"}
    ledger = (
        json.dumps(opened, sort_keys=True) + "\n" + json.dumps(closed, sort_keys=True) + "\n"
    ).encode()
    write(
        "assay-pipeline",
        {
            "summary": ("docs/results/main-fixture/metrics.json", dump(summary)),
            "gates": ("gates.yaml", gates),
            "experiments": ("ledger/experiments.jsonl", ledger),
        },
        repo="biplob819/assay-pipeline",
        evalkit_pin="evalkit-harness==0.1.0",
        lessons="docs/lessons.md",
    )


def respectful_offers() -> None:
    """respectful-offers: a simulated run, NTD units, policy and variant labels, no models."""
    ms = []
    for policy, v in (("bandit", 41.5), ("rule_following_baseline", 33.0), ("status_quo", 30.2)):
        for variant, d in (("central", 0.0), ("conservative", -2.0)):
            lab = {"policy": policy, "variant": variant}
            ms.append(
                metric(
                    "incremental_retained_per_1000",
                    "ratio",
                    v + d,
                    ci_low=v + d - 3.1,
                    ci_high=v + d + 2.9,
                    ci_method=BOOT,
                    labels=lab,
                )
            )
            ms.append(
                metric(
                    "ltv_proxy_per_member",
                    "NTD",
                    812.4 + d,
                    ci_low=790.0 + d,
                    ci_high=834.0 + d,
                    ci_method=BOOT,
                    labels=lab,
                )
            )
            for rule in ("max_one_offer_a_month", "no_offer_after_opt_out"):
                ms.append(
                    metric(
                        "violations", "count", 0, labels={**lab, "respectful": True, "rule": rule}
                    )
                )
    for variant in ("central", "conservative"):
        lab = {"a": "bandit", "b": "rule_following_baseline", "variant": variant}
        ms.append(
            metric(
                "diff.incremental_retained_per_1000_ntd",
                "ratio",
                0.011,
                ci_low=0.004,
                ci_high=0.018,
                ci_method="paired bootstrap, Holm",
                labels=lab,
            )
        )
        ms.append(metric("separated.incremental_retained_per_1000_ntd", "bool", True, labels=lab))
    ms.append(metric("churn.brier", "ratio", 0.081))
    ms.append(metric("churn.ece", "ratio", 0.012))
    summary = {
        "schema_version": "metrics-1",
        "project": "respectful-offers",
        "run_id": "kkbox-fixture",
        "created_at": "2026-11-17T16:45:00+05:30",
        "simulated": True,
        "provenance": {
            "code_version": "fixture",
            "snapshot": "fixture",
            "config_hash": "fixture",
            "evalkit": None,
        },
        "metrics": ms,
    }
    gates = (
        b"gates:\n"
        b"  - metric: violations\n"
        b"    labels: {respectful: true}\n"
        b'    op: "=="\n'
        b"    value: 0\n"
        b"  - metric: diff.incremental_retained_per_1000_ntd\n"
        b"    labels: {a: bandit, b: rule_following_baseline, variant: central}\n"
        b"    bound: lower\n"
        b'    op: ">="\n'
        b"    value: 0\n"
        b"  - metric: separated.incremental_retained_per_1000_ntd\n"
        b"    labels: {a: bandit, b: rule_following_baseline, variant: conservative}\n"
        b'    op: "=="\n'
        b"    value: true\n"
    )
    write(
        "respectful-offers",
        {
            "summary": ("results/kkbox/metrics.json", dump(summary)),
            "gates": ("configs/gates.yaml", gates),
        },
        repo="biplob819/respectful-offers",
        evalkit_pin="evalkit-harness>=0.3,<0.4",
    )


def agentic_backend(name: str, fixes: list[str], acc: float, strict: float) -> dict[str, Any]:
    return {
        "backend": name,
        "fixes": fixes,
        "trials": 3,
        "accuracy": iv(acc, round(acc - 0.07, 4), round(min(acc + 0.06, 1.0), 4)),
        "accuracy_strict": iv(strict, round(strict - 0.08, 4), round(min(strict + 0.07, 1.0), 4)),
        "by_category": [
            {"category": "aggregation", "n": 10, "accuracy": iv(0.9, 0.7, 1.0)},
        ],
        "cost_per_correct_usd": 0.01,
        "latency_p50_ms": 4000,
    }


def agentic_summary() -> dict[str, Any]:
    """agentic-analytics' own summary.json shape: schema_version 1.0, a `backends` list with
    one row per backend and fix set; the published headline reads the rows with no fixes."""
    return {
        "schema_version": "1.0",
        "run_id": "rescore-fixture",
        "config_hash": "fixture",
        "as_of": "2026-11-17",
        "pack": "fixture@1.0",
        "gold_mode": "cross_engine",
        "backends": [
            agentic_backend("claude_mcp", [], 0.7, 0.7),
            agentic_backend("claude_mcp", ["descriptions"], 0.8, 0.75),
            agentic_backend("gpt_mcp", [], 0.9, 0.85),
            agentic_backend("gpt_mcp", ["descriptions", "examples"], 0.92, 0.9),
            agentic_backend("cortex_analyst", [], 0.8, 0.6),
        ],
        "uplifts": [],
        "probes": {},
        "cost_status": "reconciled",
        "total_cost_usd": 1.0,
        "started_at": "2026-11-17T08:00:00Z",
        "finished_at": "2026-11-17T09:00:00Z",
    }


def agentic() -> None:
    """agentic-analytics: its own summary.json, read through the config's path selectors."""
    write(
        "agentic-analytics",
        {"summary": ("docs/results/summary.json", dump(agentic_summary()))},
        repo="biplob819/agentic-analytics",
    )


def fraud_summary() -> dict[str, Any]:
    """fraud-review-bedrock's own summary-eval2.json shape (schema_version 1.0): figures as
    {k, n, point, lo, hi} objects, `data_label` naming simulated data. No alert ids."""
    return {
        "schema_version": "1.0",
        "run_id": "eval2-fixture",
        "split": "eval",
        "data_label": "simulated",
        "n_alerts": 300,
        "n_frauds": 40,
        "leakage": {"k": 0, "n": 40, "point": 0.0, "lo": 0.0, "hi": 0.0721575245055146},
        "recall_end_to_end": {"k": 40, "n": 40, "point": 1.0, "lo": 0.912, "hi": 1.0},
        "auto_clear_rate": {"k": 30, "n": 300, "point": 0.1, "lo": 0.07, "hi": 0.14},
        "minutes_saved_per_1000": {"point": 300.0, "lo": 200.0, "hi": 400.0},
        "thresholds": [{"metric": "leakage", "op": "==", "value": 0.0, "passed": True}],
        "started_at": "2026-11-17T08:00:00.000000Z",
        "finished_at": "2026-11-17T08:10:00Z",
    }


def visibility_summary() -> dict[str, Any]:
    """ai-visibility's own study summary shape: a `share` list of rows keyed by kind,
    publisher, model, run and intent (null for all intents), each with a share figure."""
    rows = []
    for model, k in (("claude-sonnet-5-5", 12), ("gpt-6.1-sol", 70)):
        for run in (1, 2):
            for intent in (None, "fitness"):
                for kind in ("presence", "consulted"):
                    rows.append(
                        {
                            "intent": intent,
                            "kind": kind,
                            "model": model,
                            "publisher": "nhs.uk",
                            "run": run,
                            "share": {
                                "hi": round(k / 100 + 0.08, 2),
                                "k": k,
                                "lo": round(k / 100 - 0.07, 2),
                                "n": 100,
                                "point": k / 100,
                            },
                        }
                    )
    return {
        "schema_version": "1.0",
        "config_hash": "fixture",
        "generated_at": "2026-11-16T20:00:00Z",
        "share": rows,
        "citations": [],
        "comparisons": [],
        "spend_usd": 1.0,
    }


def shapes() -> None:
    """Own-format summaries for the path selector tests (snapshotted in tmp_path there)."""
    folder = HERE / "shapes"
    folder.mkdir(exist_ok=True)
    for name, data in (
        ("fraud-review-bedrock.json", fraud_summary()),
        ("ai-visibility.json", visibility_summary()),
    ):
        (folder / name).write_bytes(dump(data))


def fraud() -> None:
    """fraud-review-bedrock: the fetch failed (recorded by the snapshot step)."""
    write(
        "fraud-review-bedrock",
        {},
        repo="biplob819/fraud-review-bedrock",
        commit=None,
        status="missing",
        reason="git fetch failed (exit 128)",
    )


REGISTRY_ROWS = [
    ("project", "repo", "branch", "pin", "install", "tests", "summary", "deselect", "known",
     "note"),
    ("agentic-analytics", "biplob819/agentic-analytics", "main", "-", "", "auto",
     "docs/results/summary.json", "-", "-", "own summary format"),
    ("ai-visibility", "biplob819/ai-visibility", "main", "-", "", "auto", "-", "-", "-",
     "no summary registered"),
    ("assay-pipeline", "biplob819/assay-pipeline", "main", "evalkit-harness==0.1.0", "", "auto",
     "-", "-", "-", "metrics-1 (path in the index config)"),
    ("course-assistant", "biplob819/course-assistant", "main", "evalkit-harness>=0.2,<0.3", "",
     "auto", "tests/eval/evalreg/summary.json", "-", "-", "EvalKit summary"),
    ("fraud-review-bedrock", "biplob819/fraud-review-bedrock", "main", "-", "", "all",
     "docs/results/summary.json", "-", "-", "fetch fails"),
    ("gemma-grounded-health", "biplob819/gemma-grounded-health", "main", "-", "", "auto",
     "docs/results/main/summary.json", "-", "-", "EvalKit summary before 0.3"),
    ("respectful-offers", "biplob819/respectful-offers", "main", "evalkit-harness>=0.3,<0.4", "",
     "auto", "results/kkbox/metrics.json", "-", "-", "metrics-1, simulated"),
]  # fmt: skip
REGISTRY = "".join("\t".join(row) + "\n" for row in REGISTRY_ROWS)


def main() -> None:
    if SNAPS.exists():
        for p in sorted(SNAPS.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
    (HERE / "downstream.tsv").write_text(REGISTRY, encoding="utf-8", newline="\n")
    evalkit_self()
    course_assistant()
    gemma()
    assay()
    respectful_offers()
    agentic()
    fraud()
    shapes()


if __name__ == "__main__":
    main()
