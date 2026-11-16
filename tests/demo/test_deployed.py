"""DEM-06: the deployed demo. The record in docs/measurements is written from checks against
the public URL and the spend store; these tests hold it to the repository, so a change that
breaks what was deployed (a renamed secret, a moved entry point, a committed secrets file) is
caught. Set EVALKIT_CHECK_DEMO=1 to also check that the URL answers (never in CI)."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

import pytest

from evalkit.demo import page as P

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs" / "measurements" / "demo-deploy-2026-11-17.json"
LEDGER = ROOT / "docs" / "measurements" / "spend-ledger.jsonl"


def _record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_the_deployed_app_runs_from_this_repo():
    rec = _record()
    for key in ("main_file", "requirements", "config"):
        assert (ROOT / rec[key]).is_file(), rec[key]
    config = (ROOT / rec["config"]).read_text(encoding="utf-8")
    assert 'showErrorDetails = "none"' in config and "gatherUsageStats = false" in config
    assert rec["url"].startswith("https://") and rec["url"].endswith(".streamlit.app/")


def test_secrets_live_in_the_host_store_not_the_repo():
    rec = _record()
    assert tuple(rec["secrets_in_host_store"]) == P.SECRETS
    app = (ROOT / rec["main_file"]).read_text(encoding="utf-8")
    assert "st.secrets" in app  # read from the host's store at run time
    assert ".streamlit/secrets.toml" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.splitlines()
    assert not [f for f in tracked if f.endswith(("secrets.toml", ".env"))]


def test_the_deployed_checks_passed():
    rec = _record()
    checks = {c["check"]: c["result"] for c in rec["checks"]}
    assert set(checks.values()) == {"pass"}
    for needed in ("replay mode", "budget line", "wrong passcode", "own-keys", "live request"):
        assert any(needed in c for c in checks), needed


def test_the_live_check_spent_within_the_request_cap():
    live = _record()["live_check"]
    assert 0 < live["spent_usd"] <= live["per_request_cap_usd"] == P.PER_REQUEST_CAP_USD
    assert round(live["budget_before_usd"] - live["spent_usd"], 2) == live["budget_after_usd"]
    rows = [json.loads(ln) for ln in LEDGER.read_text(encoding="utf-8").splitlines() if ln]
    row = next(r for r in rows if r.get("run_id") == live["ledger_run_id"])
    assert row["spent_usd"] == live["spent_usd"] and row["kind"] == "demo_live"


@pytest.mark.skipif(not os.environ.get("EVALKIT_CHECK_DEMO"), reason="network check, opt-in")
def test_the_demo_url_answers():
    import http.cookiejar

    # Streamlit Cloud sets a cookie and redirects back; without a cookie jar that loops
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    urllib.request.install_opener(opener)
    try:
        with urllib.request.urlopen(_record()["url"], timeout=30) as resp:
            assert resp.status == 200
    finally:
        urllib.request.install_opener(None)  # type: ignore[arg-type]
