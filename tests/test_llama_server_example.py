"""GEM-01, GEM-02 (B5.17): the "Gemma through llama-server" worked example in docs/plugins.md
and examples/llama-server/evalkit.yaml. No network: the config is loaded and routed only.

The expected values are Gemma Grounded Health's own, read from its configs/evalkit_v2.yaml and
src/ggh/serve/llama.py at its commit 22579b3 (GGH OWNER D-64).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evalkit.config import load_run_config
from evalkit.pipeline.generate import adapter_key, generate_inputs, provider_for
from evalkit.schemas import Case, TaskConfig

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "llama-server" / "evalkit.yaml"
PLUGINS = ROOT / "docs" / "plugins.md"
MODEL = "gemma-e2b-grounded-v2-q4"
# ggh serve's arguments (src/ggh/serve/llama.py, server_args), the model path aside
GGH_FLAGS = [
    ("--host", "127.0.0.1"),
    ("--port", "8083"),
    ("--n-gpu-layers", "999"),
    ("--parallel", "1"),
    ("--seed", "7"),
    ("--threads", "8"),
    ("--ctx-size", "8192"),
    ("--temp", "0"),
    ("--top-k", "1"),
    ("--jinja", None),
    ("--reasoning", "off"),
    ("--reasoning-budget", "0"),
    ("--no-webui", None),
]


def _section() -> str:
    text = PLUGINS.read_text(encoding="utf-8")
    start = text.index("### Worked example: Gemma through llama-server")
    end = text.index("\n## ", start)
    return text[start:end]


def _fences(section: str, lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", section, flags=re.S)


def _server_args() -> list[str]:
    cmd = next(b for b in _fences(_section(), "bash") if b.startswith("llama-server"))
    return cmd.replace("\\\n", " ").split()


def _task() -> TaskConfig:
    return TaskConfig(
        name="t",
        dataset="d",
        split="s",
        labels=("yes", "no", "maybe"),
        prompt_template="{question}",
        output_schema="answer",
        scorers=("label_correct",),
    )


# ---- GEM-01: the worked example ------------------------------------------------------------


def test_plugins_doc_names_the_route_and_the_model():
    s = _section()
    for needle in (
        MODEL,
        "`endpoints`",
        "`openai_compatible`",
        "llama-server",
        "b12421",
        "https://huggingface.co/biplob123/gemma-4-e2b-grounded-health",
        "https://huggingface.co/biplob123/gemma-4-e2b-grounded-health-GGUF",
        "https://biplob819.github.io/evalkit/gemma/",
    ):
        assert needle in s, needle


def test_plugins_doc_server_command_has_every_ggh_flag():
    args = _server_args()
    assert args[0] == "llama-server" and args[1] == "--model"
    for flag, value in GGH_FLAGS:
        assert flag in args, flag
        if value is not None:
            assert args[args.index(flag) + 1] == value, flag
    named = {a for a in args if a.startswith("--")}
    assert named == {f for f, _ in GGH_FLAGS} | {"--model"}  # nothing GGH did not pass


def test_plugins_doc_states_every_gotcha():
    s = " ".join(_section().split())
    for needle in (
        "--reasoning-budget 0",
        "--think=false",
        "EvalKit sends no sampling parameters",
        "--temp 0 --top-k 1 --seed 7",
        "--parallel 1",
        "concurrency: {openai_compatible: 1}",
        "the endpoint URL and the GGUF behind it are not part of it",
        "Give every GGUF its own model name",
    ):
        assert needle in s, needle


def test_plugins_doc_serves_locally_and_sends_no_key():
    args = _server_args()
    assert args[args.index("--host") + 1] == "127.0.0.1"
    cfg = load_run_config(EXAMPLE)
    assert all(u.startswith("http://127.0.0.1:") for u in cfg.endpoints.values())
    assert "no API key is sent" in _section()


# ---- GEM-02: the example config ------------------------------------------------------------


def test_example_config_loads_and_matches_ggh():
    cfg = load_run_config(EXAMPLE)
    assert cfg.models == (MODEL,)
    assert dict(cfg.endpoints) == {MODEL: "http://127.0.0.1:8083/v1"}
    assert cfg.judges == ("gpt-6.1-sol",)
    assert cfg.extractor == "gpt-6-luna"
    assert cfg.structured_output is True
    assert cfg.max_output_tokens == 2048
    assert cfg.seed == 7
    assert dict(cfg.concurrency) == {"openai": 8, "openai_compatible": 1}
    assert (cfg.default_effort, cfg.judge_effort, cfg.extractor_effort) == ("medium",) * 3
    assert cfg.max_cost_usd == 16


def test_example_routes_to_openai_compatible_without_effort():
    cfg = load_run_config(EXAMPLE)
    assert provider_for(MODEL, cfg.endpoints, cfg.systems) == "openai_compatible"
    assert adapter_key("openai_compatible", MODEL) == f"openai_compatible:{MODEL}"
    case = Case(id="c1", question="q?", context=("a.",), reference_label="yes", source="t")
    inputs = generate_inputs([case], _task(), cfg)
    assert [i.provider for i in inputs] == ["openai_compatible"]
    assert inputs[0].effort is None  # not under `efforts`, so no reasoning effort is sent


def test_example_sends_one_request_at_a_time_and_caps_cost():
    cfg = load_run_config(EXAMPLE)
    assert cfg.concurrency["openai_compatible"] == 1
    assert cfg.max_cost_usd is not None and cfg.max_cost_usd > 0


def test_docs_yaml_block_is_the_example_file():
    blocks = _fences(_section(), "yaml")
    assert blocks == [EXAMPLE.read_text(encoding="utf-8")]


@pytest.mark.parametrize("model", ["gemma-e2b-grounded-v2-q4"])
def test_an_unlisted_gemma_name_is_refused_not_guessed(model):
    with pytest.raises(ValueError, match="endpoints"):
        provider_for(model, {}, {})


def test_example_dry_run_plans_the_endpoint_at_zero_cost(tmp_path, monkeypatch, capsys):
    """`evalkit run examples/llama-server/evalkit.yaml --dry-run`, as the doc shows it, with the
    network refused and PubMedQA replaced by the repo's mini fixture."""
    import json

    from evalkit import cli
    from evalkit import run as R
    from evalkit.datasets import parse_pqa_l

    def refuse(*a, **k):
        raise OSError("network disabled in test")

    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-docs-NEVER-PRINT")
    mini = json.loads((ROOT / "tests" / "fixtures" / "pubmedqa_mini.json").read_text("utf-8"))
    monkeypatch.setattr(R, "load_pqa_l", lambda: parse_pqa_l(mini))
    n = len(parse_pqa_l(mini))
    argv = ["run", str(EXAMPLE), "--dry-run", "--n", str(n), "--json"]
    argv += ["--out", str(tmp_path / "runs"), "--store", str(tmp_path / "store")]
    with pytest.warns(UserWarning, match=MODEL):  # no price: costed at $0 with a warning
        assert cli.main(argv) == 0
    plan = json.loads(capsys.readouterr().out)
    lines = {(x["node"], x["model"]): x for x in plan["estimate"]["lines"]}
    assert lines[("generate", MODEL)] == {
        "calls": n,
        "model": MODEL,
        "node": "generate",
        "usd": 0.0,
    }
    assert ("extract_claims", "gpt-6-luna") in lines and ("judge", "gpt-6.1-sol") in lines
    assert plan["dry_run"] is True
