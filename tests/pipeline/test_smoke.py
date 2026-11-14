import asyncio
import json
from pathlib import Path

import pytest

from evalkit.config import load_run_config
from evalkit.cost import TokenAssumptions, load_prices
from evalkit.smoke import assumptions_from, estimate, run_smoke
from evalkit.tasks import builtin_task
from tests.pipeline.fakes import ScriptedAdapter
from tests.pipeline.test_generate import CASES, GOOD

ROOT = Path(__file__).resolve().parents[2]
PRICES = load_prices()


def test_reference_config_loads_with_explicit_efforts():
    cfg = load_run_config(ROOT / "configs" / "pubmedqa.yaml")
    assert cfg.models == ("claude-sonnet-5-5", "claude-haiku-5-5", "gpt-6.1-sol", "gpt-6-luna")
    assert cfg.default_effort == "medium" and cfg.judge_effort == "low"
    assert all(m in PRICES for m in (*cfg.models, *cfg.judges, cfg.extractor))


def test_smoke_estimate_covers_generation_only_and_is_cheap():
    cfg = load_run_config(ROOT / "configs" / "pubmedqa.yaml")
    est = estimate(PRICES, cfg, 20)
    assert {line.node for line in est.lines} == {"generate"}
    assert est.total_usd < 0.5


def test_smoke_run_writes_measurements_that_feed_the_cost_model(tmp_path):
    cfg = load_run_config(ROOT / "configs" / "pubmedqa.yaml")
    fake = ScriptedAdapter(lambda req: GOOD if "haiku" not in req.model else "nonsense")
    out = tmp_path / "smoke.json"
    stats = asyncio.run(
        run_smoke(
            config=cfg,
            task=builtin_task("pubmedqa"),
            cases=CASES,
            adapters={"anthropic": fake, "openai": fake},
            prices=PRICES,
            store_dir=tmp_path / "store",
            out_path=out,
            max_cost_usd=1.0,
        )
    )
    assert stats["claude-haiku-5-5"]["errors"] == {"schema": 3}
    assert stats["gpt-6.1-sol"]["avg_tokens_reasoning"] == 200.0
    meta = json.loads(out.read_text())["meta"]
    assert meta["n_cases"] == 3 and meta["structured_output"] is True
    a = assumptions_from(out)
    assert isinstance(a, TokenAssumptions) and (a.t_in, a.t_out, a.t_reason) == (600, 60, 200)
    assert a.t_j_reason == TokenAssumptions().t_j_reason


@pytest.mark.safety
def test_smoke_script_refuses_without_keys_and_never_prints_them(monkeypatch, capsys, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_run", ROOT / "scripts" / "smoke_run.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)  # no .env there
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = mod.main(["--n", "2", "--config", str(ROOT / "configs" / "pubmedqa.yaml"), "--yes"])
    out = capsys.readouterr().out
    assert code == 2 and "missing API keys" in out and "estimate $" in out
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "Q" * 40)  # secret-scan: allow
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-" + "Q" * 40)  # secret-scan: allow
    code = mod.main(["--n", "2", "--config", str(ROOT / "configs" / "pubmedqa.yaml")])
    out = capsys.readouterr().out
    assert code == 0 and "dry run" in out and "QQQQ" not in out
