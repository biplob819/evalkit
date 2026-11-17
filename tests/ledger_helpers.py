"""Shared synthetic fixtures for the B5.7 ledger tests (no network, no keys, no model call).

The downstream shapes follow specs/B5.7-ledger/inputs.md: fraud-review-bedrock's run history
(leakage, auto-clear, cost per 1,000 alerts), respectful-offers' churn block, course-assistant's
per-call spend lines and assay-pipeline's hand-written ones. All values are made up.
"""

from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
LEDGER_FIX = FIX / "ledger"
HELDOUT_FIX = FIX / "heldout"
MANIFEST = HELDOUT_FIX / "MANIFEST.json"
HELD_IDS = ("fx-h1", "fx-h2", "fx-h3")
GOLDEN_SUMMARY = FIX / "golden" / "summary_pubmedqa_mini.json"
EXAMPLE_RUN = ROOT / "src" / "evalkit" / "examples" / "example_run"
EVALKIT_SPEND = ROOT / "docs" / "measurements" / "spend-ledger.jsonl"
FRAUD = "fraud-review-bedrock"


def at(day: int, hour: int = 0) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:00:00Z"


@pytest.fixture
def no_network(monkeypatch):
    """Ledgers never open a connection and need no key; any attempt fails the test."""

    def refuse(*a, **k):
        raise AssertionError("the ledger must not touch the network")

    real_connect = socket.socket.connect

    def connect(sock, address, *a, **k):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1", "localhost"}:
            return real_connect(sock, address, *a, **k)
        return refuse()

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    monkeypatch.setattr("evalkit.ledger._io.evalkit_version", lambda: "0.0.0-test")


def metrics_doc(
    rows: list[dict[str, Any]],
    *,
    project: str = FRAUD,
    run_id: str = "r1",
    created_at: str = "2026-11-17T00:00:00Z",
    simulated: bool = False,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "metrics-1",
        "project": project,
        "run_id": run_id,
        "created_at": created_at,
        "simulated": simulated,
        "metrics": rows,
    }
    if provenance is not None:
        doc["provenance"] = provenance
    return doc


def write_run(
    folder: Path,
    rows: list[dict[str, Any]],
    *,
    settings: dict[str, Any] | None = None,
    **top: Any,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "metrics.json"
    path.write_text(json.dumps(metrics_doc(rows, **top), sort_keys=True), encoding="utf-8")
    if settings is not None:
        (folder / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return path


def rate(name: str, k: int, n: int, **labels: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"name": name, "unit": "rate", "value": k / n, "k": k, "n": n}
    if labels:
        row["labels"] = labels
    return row


def fraud_rows(leaked: int, cleared: int, cost: float, n: int = 400) -> list[dict[str, Any]]:
    """fraud-review-bedrock's run-history figures (its runs.jsonl) as metrics-1 rows."""
    return [
        rate("leakage", leaked, n),
        rate("auto_clear_rate", cleared, n),
        {"name": "cost_per_1000_usd", "unit": "USD", "value": cost},
        {"name": "latency_p95_stage2_ms", "unit": "ms", "value": 2400},
    ]


def fraud_run(tmp: Path, i: int, leaked: int, cleared: int, cost: float) -> Path:
    return write_run(
        tmp / f"run{i:02d}",
        fraud_rows(leaked, cleared, cost),
        run_id=f"fr-{i:02d}",
        created_at=at(i + 1),
        provenance={"code_version": f"c{i:02d}", "config_hash": "cfg-1"},
    )


FRAUD_HISTORY = [  # (leaked of 400, auto-cleared of 400, cost per 1,000 alerts)
    (1, 212, 4.10),
    (2, 208, 4.22),
    (1, 215, 4.05),
    (0, 210, 4.18),
    (2, 206, 4.12),
    (1, 211, 4.20),
]


def git(root: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "core.autocrlf=false",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=check,
    )
    return done.stdout.strip()


def repo(tmp: Path) -> Path:
    root = tmp / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def evalkit_summary(run_id: str, *, pending: bool = False) -> dict[str, Any]:
    d = json.loads(GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    d["run_id"] = run_id
    if pending:
        d["judging_status"], d["finished_at"], d["self_preference"] = "pending", None, {}
        d["comparisons"] = [c for c in d["comparisons"] if c["metric"] != "headline"]
        for m in d["models"]:
            m["headline"], m["groundedness_mean"] = {}, {}
    return d


def example_summary() -> dict[str, Any]:
    return json.loads((EXAMPLE_RUN / "summary.json").read_text(encoding="utf-8"))


def efficiency_run(
    tmp: Path,
    i: int,
    *,
    cost: float,
    cache: float,
    p95: float,
    price_date: str = "2026-11-14",
) -> Path:
    """An EvalKit run folder whose summary.json carries one model's efficiency block (EFF-02)."""
    d = copy.deepcopy(example_summary())
    d["run_id"] = f"eff-{i:02d}"
    d["models"] = [d["models"][0]]
    d["comparisons"] = []
    d["self_preference"] = {}
    eff = d["models"][0]["efficiency"]
    eff["cost_per_case_usd"] = {"point": cost, "lo": cost, "hi": cost}
    eff["cache_hit_share"] = cache
    eff["latency_p95_ms"] = {"point": p95, "lo": p95, "hi": p95}
    eff["price_date"] = price_date
    folder = tmp / f"eff{i:02d}"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(json.dumps(d, sort_keys=True), encoding="utf-8")
    return folder
