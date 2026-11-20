#!/usr/bin/env python3
"""Publish a finished run as the project's headline (B2.3, OWNER D-47).

    uv run python scripts/publish_results.py runs/r-XXXX

Copies the run's summary, report and charts (and the labeller record, when a model made the
calibration labels) into docs/results/, draws docs/assets/headline.svg
and regenerates the README headline block from the summary. The Pages workflow deploys
docs/results/ to https://biplob819.github.io/evalkit/ when it lands on main.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_URL = "https://biplob819.github.io/evalkit/"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--root", type=Path, default=ROOT, help="repository root (tests use a copy)")
    args = ap.parse_args(argv)
    root = args.root

    from evalkit.report.readme import readme_block, readme_chart, splice
    from evalkit.run import read_labeller
    from evalkit.schemas import RunSummary

    run_dir = args.run_dir
    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    if summary.judging_status != "complete":
        print("refusing to publish: judging is still pending", file=sys.stderr)
        return 2
    n = json.loads((run_dir / "run.json").read_text("utf-8"))["n"]
    labeller = read_labeller(run_dir)  # who made the calibration labels (D-81)
    results = root / "docs" / "results"
    results.mkdir(parents=True, exist_ok=True)
    for name in ("summary.json", "report.html", "charts.vl.json", "run.json"):
        shutil.copyfile(run_dir / name, results / name)
    # Who made the calibration labels travels with the results, so the README can be rebuilt
    # from committed files alone (D-81); a stale one from an earlier run is removed.
    published_labeller = results / "labeller.json"
    if labeller is not None:
        shutil.copyfile(run_dir / "calibration" / "labeller.json", published_labeller)
    else:
        published_labeller.unlink(missing_ok=True)
    assets = root / "docs" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "headline.svg").write_text(readme_chart(summary, labeller), encoding="utf-8")
    readme = root / "README.md"
    readme.write_text(
        splice(readme.read_text("utf-8"), readme_block(summary, n, REPORT_URL, labeller)),
        encoding="utf-8",
    )
    print(f"published {summary.run_id} ({n} cases) to docs/results and README")
    return 0


if __name__ == "__main__":
    sys.exit(main())
