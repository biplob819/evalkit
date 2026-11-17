"""B5.12 per-sentence citation support (CIT-01..03). Scripted adapters only: no network, no
spend. The pinned values below were computed on the code before B5.12 (commit 6c42cc8)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import math
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from evalkit import cli
from evalkit import run as R
from evalkit.cost import load_prices
from evalkit.datasets.pubmedqa import parse_pqa_l
from evalkit.pipeline import judge as jm
from evalkit.pipeline.aggregate import aggregate, citation_counts
from evalkit.pipeline.answer import SchemaError
from evalkit.pipeline.judge import (
    BLIND_DENYLIST,
    CITATION_PROMPT_HASH,
    JUDGE_PROMPT_HASH,
    CitationResult,
    JudgeSelection,
    JudgingResult,
    citation_input,
    citation_schema,
    judge_citations,
    judge_citations_live,
    judge_claims,
    parse_citations,
    render_citation_prompt,
)
from evalkit.pipeline.task_scorers import BENCHMARK_SCORERS, resolve_task_scorers
from evalkit.report.html import _model_table
from evalkit.runtime import Executor, Store
from evalkit.runtime.canonical import canonical_json, sha256_hex
from evalkit.schemas import (
    Case,
    CitationVerdict,
    Claim,
    ClaimVerdict,
    Interval,
    Record,
    RunConfig,
    RunSummary,
)
from evalkit.schemas.benchmark import CITATION_ROLES
from evalkit.stats import Resamples
from evalkit.tasks import builtin_task
from tests.golden.test_golden_run import MINI, reply
from tests.pipeline.fakes import ScriptedBatchAdapter

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "citation"
PRICES = load_prices()
CFG = RunConfig(
    models=("claude-sonnet-5-5", "gpt-6.1-sol"),
    judges=("claude-opus-5-5", "gpt-6.1-sol"),
    extractor="claude-haiku-5-5",
)
JUDGES = CFG.judges
T0 = datetime(2026, 11, 17, 9, tzinfo=UTC)

# ---- CIT-01 the claim judge is unchanged (values pinned before the change) ---------------------

PIN_CASE = Case(
    id="pqa-1",
    question="q",
    context=("Readmissions fell by 12% in the Claude cohort.", "Costs rose.", "N=400."),
    reference_label="yes",
    source="t",
)
PIN_CLAIM = Claim(
    id="pqa-1:m:0", text="GPT-4 says readmissions fell.", cited=(1, 3), extractor_version="ext2-x"
)
PINNED = {
    "JUDGE_PROMPT_HASH": "9a3a8246049f6f85",
    "cache_key": "5daad1eb7f41e4bf5dd14c6b79c26659268ceef7306c7634b1b171ef883a73b2",
    "custom_id": "j5daad1eb7f41e4bf5dd14c6b79c26659268ceef7",
    "request": "b298b9fde7360fc5ef8ff0a011c9e6568f5999335554986fa5b570632fd4c8c8",
    "prompt": "248746704f762a28cf21c4e61a680f604af8d92471815f094a337d20b3a51595",
    "schema": "a1001b2af5c369d4094c15f200dcce4cec5027d1cd2290d0feb85a79432fb6bb",
}


def test_claim_judge_identity_is_pinned_from_before_the_change():
    inp = jm.judge_input(PIN_CLAIM, PIN_CASE, "claude-opus-5-5", "medium")
    key = jm._cache_key(inp)
    req = jm._request(inp)
    assert PINNED["JUDGE_PROMPT_HASH"] == jm.JUDGE_PROMPT_HASH
    assert key == PINNED["cache_key"]
    assert jm.custom_id_for(key) == PINNED["custom_id"]
    assert sha256_hex(req) == PINNED["request"]
    assert hashlib.sha256(req.prompt.encode()).hexdigest() == PINNED["prompt"]
    assert sha256_hex(jm.verdict_schema()) == PINNED["schema"]
    # The default rubric is today's claim judge, value for value.
    claims = jm.CLAIMS
    assert claims.cache_key(inp) == key and claims.custom_id(key) == PINNED["custom_id"]
    assert claims.request(inp) == req and (claims.node, claims.meta) == ("judge", "judge_batch")


def test_citation_prompt_hash_is_separate_and_pinned():
    parts = [
        jm.CITATION_RUBRIC,
        citation_schema(),
        list(CITATION_ROLES),
        jm.SENTENCE_FORMAT,
        jm.CITATION_CODE_VERSION,
        jm._BLIND_RE.pattern,
        jm._SPACED_RE.pattern,
        sorted(jm._STRONG),
        jm.CITATION_MAX_OUTPUT_TOKENS,
        sorted(jm._BLIND_FOLD.items()),
    ]
    assert hashlib.sha256(canonical_json(parts).encode()).hexdigest()[:16] == CITATION_PROMPT_HASH
    assert CITATION_PROMPT_HASH == "be34ebe577bc96ae"  # pinned: a prompt edit must update this
    assert CITATION_PROMPT_HASH != JUDGE_PROMPT_HASH
    inp = citation_input(PIN_CLAIM, PIN_CASE, "claude-opus-5-5", "medium")
    with pytest.raises(ValueError, match="CITATION_PROMPT_HASH"):
        jm.CitationInput.model_validate({**inp.model_dump(), "rubric_hash": JUDGE_PROMPT_HASH})


def test_citation_keys_ids_and_store_never_collide_with_claim_verdicts():
    claim_inp = jm.judge_input(PIN_CLAIM, PIN_CASE, "claude-opus-5-5", "medium")
    cite_inp = citation_input(PIN_CLAIM, PIN_CASE, "claude-opus-5-5", "medium")
    ck, kk = jm.CLAIMS.cache_key(claim_inp), jm.CITATIONS.cache_key(cite_inp)
    assert ck != kk
    assert kk == sha256_hex(
        {"node": "citation_judge", "code_version": jm.CITATION_CODE_VERSION, "input": cite_inp}
    )
    cid = jm.CITATIONS.custom_id(kk)
    assert cid.startswith("c") and jm.CLAIMS.custom_id(ck).startswith("j")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cid) and "pqa" not in cid
    assert (jm.CITATIONS.node, jm.CITATIONS.meta, jm.CITATIONS.lock) == (
        "citation_judge",
        "citation_batch",
        "citation",
    )
    assert jm.CITATIONS.meta != jm.CLAIMS.meta and jm.CITATIONS.lock != jm.CLAIMS.lock


@pytest.mark.safety
def test_citation_prompt_numbers_each_cited_sentence_alone_redacted_and_blind():
    claim = Claim(
        id="c:m:0", text="GPT-4 and Claude agree.", cited=(3, 1, 3), extractor_version="x"
    )
    for judge in JUDGES:
        inp = citation_input(claim, PIN_CASE, judge, "medium")
        assert inp.cited == (3, 1)  # each cited sentence once, in citation order
        prompt = render_citation_prompt(inp)
        assert "[3] N=400.\n[1] Readmissions fell by 12% in the [model] cohort." in prompt
        assert "Costs rose." not in prompt  # sentence 2 is not cited
        assert "Claim: [model] and [model] agree." in prompt
        assert judge not in prompt
        for name in BLIND_DENYLIST:
            assert name not in prompt.casefold(), name
        req = jm.CITATIONS.request(inp)
        assert req.model == "citation_judge" and req.model_id == judge
        assert canonical_json(req.json_schema) == canonical_json(citation_schema())
        assert req.params == {"effort": "medium"}


# ---- CIT-01 parsing -----------------------------------------------------------------------------

CITE_13 = Claim(id="pqa-1:m:0", text="Readmissions fell.", cited=(1, 3), extractor_version="x")


def _sent(*entries) -> str:
    keys = ("n", "supports", "role")
    return json.dumps({"sentences": [dict(zip(keys, e, strict=True)) for e in entries]})


@pytest.mark.parametrize(
    "raw",
    [
        _sent((1, True, "result"), (3, False, "aim")),
        _sent((3, False, "aim"), (1, True, "result")),  # any order
        "```json\n" + _sent((1, True, "result"), (3, False, "method")) + "\n```",
    ],
)
def test_parser_accepts_every_cited_sentence_once(raw):
    out = parse_citations(raw, citation_input(CITE_13, PIN_CASE, "j", "low"))
    assert [(v.n, v.claim_id) for v in out] == [(1, "pqa-1:m:0"), (3, "pqa-1:m:0")]
    assert out[0].supports is True and out[1].supports is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        json.dumps({"sentences": [], "extra": 1}),
        json.dumps({"sentences": {"n": 1}}),
        _sent((1, True, "result")),  # sentence 3 not answered
        _sent((1, True, "result"), (3, False, "aim"), (2, True, "result")),  # 2 is not cited
        _sent((1, True, "result"), (1, True, "result"), (3, False, "aim")),  # twice
        _sent(("1", True, "result"), (3, False, "aim")),  # number as a string
        _sent((True, True, "result"), (3, False, "aim")),  # bool is not a number
        _sent((1.0, True, "result"), (3, False, "aim")),
        _sent((1, "true", "result"), (3, False, "aim")),
        _sent((1, 1, "result"), (3, False, "aim")),
        _sent((1, True, "finding"), (3, False, "aim")),  # not one of the five roles
        _sent((1, True, None), (3, False, "aim")),
        json.dumps({"sentences": [{"n": 1, "supports": True}, {"n": 3, "supports": False}]}),
        json.dumps(
            {
                "sentences": [
                    {"n": 1, "supports": True, "role": "result", "why": "x"},
                    {"n": 3, "supports": False, "role": "aim"},
                ]
            }
        ),
    ],
)
def test_parser_rejects_anything_else(raw):
    with pytest.raises(SchemaError):
        parse_citations(raw, citation_input(CITE_13, PIN_CASE, "j", "low"))


# ---- CIT-01 batch and live paths with a scripted adapter -----------------------------------------


def _shown(prompt: str) -> list[int]:
    block = prompt.split("(judge each one on its own):\n", 1)[1].split("\n\n", 1)[0]
    return [int(n) for n in re.findall(r"^\[(\d+)\] ", block, re.M)]


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def cite_reply(req) -> str:
    """Deterministic citation replies: supports and role vary with judge and sentence."""
    h = _h(req.model_id, req.prompt)
    return json.dumps(
        {
            "sentences": [
                {"n": n, "supports": (h >> i) % 2 == 0, "role": CITATION_ROLES[(h // 3 + n) % 5]}
                for i, n in enumerate(_shown(req.prompt))
            ]
        }
    )


def full_reply(req):
    return cite_reply(req) if req.model == "citation_judge" else reply(req)


ITEMS = [
    (
        PIN_CASE,
        Claim(id="pqa-1:m:0", text="Readmissions fell.", cited=(1, 3), extractor_version="x"),
    ),
    (PIN_CASE, Claim(id="pqa-1:m:1", text="Nothing cited.", cited=(), extractor_version="x")),
    (PIN_CASE, Claim(id="pqa-1:m:2", text="Cites a ghost.", cited=(1, 7), extractor_version="x")),
    (PIN_CASE, Claim(id="pqa-1:m:3", text="Costs rose.", cited=(2, 2), extractor_version="x")),
]


def _cite(ex, adapter, judge, *, live=False, **kw) -> CitationResult:
    ads = {"anthropic": adapter, "openai": adapter}
    if live:
        kw.pop("max_wait_s", None)
        return asyncio.run(judge_citations_live(ex, ITEMS, judge, CFG, ads, PRICES, run_id="r"))
    return asyncio.run(judge_citations(ex, ITEMS, judge, CFG, ads, PRICES, run_id="r", **kw))


@pytest.mark.parametrize("judge", JUDGES)
def test_batch_one_call_per_precheck_passing_claim_and_rerun_is_free(tmp_path, judge):
    fake = ScriptedBatchAdapter(cite_reply)
    res = _cite(Executor(Store(tmp_path)), fake, judge, max_wait_s=1)
    assert res.status == "complete" and res.n_errors == 0
    assert set(res.verdicts) == {"pqa-1:m:0", "pqa-1:m:3"}  # B3 failures are skipped, no call
    assert len(fake.submitted) == 1 and len(fake.submitted[0]) == 2
    assert all(br.custom_id.startswith("c") for br in fake.submitted[0])
    assert {br.request.model for br in fake.submitted[0]} == {"citation_judge"}
    assert [v.n for v in res.verdicts["pqa-1:m:0"]] == [1, 3]
    assert [v.n for v in res.verdicts["pqa-1:m:3"]] == [2]
    again = _cite(Executor(Store(tmp_path)), fake, judge, max_wait_s=1)
    assert len(fake.submitted) == 1 and again.verdicts == res.verdicts and again.cost_usd == 0


def test_live_path_shares_the_batch_cache(tmp_path):
    fake = ScriptedBatchAdapter(cite_reply)
    live = _cite(Executor(Store(tmp_path)), fake, "gpt-6.1-sol", live=True)
    assert set(live.verdicts) == {"pqa-1:m:0", "pqa-1:m:3"} and len(fake.requests) == 2
    assert fake.submitted == []
    batch = _cite(Executor(Store(tmp_path)), fake, "gpt-6.1-sol", max_wait_s=1)
    assert fake.submitted == [] and len(fake.requests) == 2 and batch.verdicts == live.verdicts


@pytest.mark.parametrize("live", [False, True])
def test_malformed_reply_gets_one_retry_then_counts_unjudged(tmp_path, live):
    calls: Counter[str] = Counter()

    def flaky(req):
        calls[req.prompt] += 1
        if "Costs rose." in req.prompt.split("\n\n")[1]:
            return "[]"  # claim 3: never parseable
        return cite_reply(req) if calls[req.prompt] > 1 else "{}"  # claim 0: fixed on retry

    fake = ScriptedBatchAdapter(flaky)
    res = _cite(Executor(Store(tmp_path)), fake, "claude-opus-5-5", live=live, max_wait_s=1)
    assert set(res.verdicts) == {"pqa-1:m:0"} and res.n_errors == 1
    assert sorted(calls.values()) == [2, 2]  # one retry each, never more
    assert math.isclose(res.cost_usd, 0.004)  # every call, retries included
    n = len(fake.requests)
    again = _cite(Executor(Store(tmp_path)), fake, "claude-opus-5-5", live=live, max_wait_s=1)
    assert len(fake.requests) == n and again.n_errors == 1  # cached as unjudged, not re-paid


def test_claim_and_citation_verdicts_live_side_by_side_in_one_store(tmp_path):
    fake = ScriptedBatchAdapter(full_reply)
    ex = Executor(Store(tmp_path))
    cites = _cite(ex, fake, "claude-opus-5-5", max_wait_s=1)
    ads = {"anthropic": fake, "openai": fake}
    claims = asyncio.run(
        judge_claims(ex, ITEMS, "claude-opus-5-5", CFG, ads, PRICES, run_id="r", max_wait_s=1)
    )
    assert len(fake.submitted) == 2  # the claim judge found nothing of its own in the cache
    assert {br.request.model for br in fake.submitted[1]} == {"judge"}
    assert all(isinstance(v, ClaimVerdict) for v in claims.verdicts.values())
    assert all(isinstance(v, tuple) for v in cites.verdicts.values())


def test_pending_citation_batch_resumes_without_paying_twice(tmp_path):
    fake = ScriptedBatchAdapter(cite_reply, pending_polls=1)
    first = _cite(Executor(Store(tmp_path)), fake, "claude-opus-5-5", max_wait_s=0)
    assert first.status == "pending" and first.verdicts == {}
    second = _cite(Executor(Store(tmp_path)), fake, "claude-opus-5-5", max_wait_s=1)
    assert second.status == "complete" and len(fake.submitted) == 1
    assert set(second.verdicts) == {"pqa-1:m:0", "pqa-1:m:3"}


# ---- end-to-end runs (scripted adapters, fixed clock) ------------------------------------------

CONFIG = (
    "models: [claude-sonnet-5-5, gpt-6.1-sol]\njudges: [claude-opus-5-5, gpt-6.1-sol]\n"
    "extractor: claude-haiku-5-5\nseed: 7\n"
)
# Fingerprints of run folders made on the code before B5.12 (sha256, first 16 hex).
BEFORE = {
    "pubmedqa": {
        "run_id": "r-49b21d83334a",
        "config_hash": "b575b3c6437d81c9",
        "records.jsonl": "0e6a06998620cb43",
        "summary.json": "d8bab8f0b252a4fb",  # D-70: + efficiency (was 799a81349c3c967b)
        "report.html": "b25d6bea9dfaebc1",  # B5.3 efficiency, D-70 wording (025a60d9cebf1d76)
        "run.json": "ca541de96c8854f1",
    },
    "loc.yaml": {
        "run_id": "r-60c5afb7c393",
        "config_hash": "b575b3c6437d81c9",
        "records.jsonl": "f6972a7f9d9c2e07",
        "summary.json": "beda9cdeccb693ce",  # D-70: + efficiency (was ba6f73735b4d229b)
        "report.html": "3aca7ba8ef03a661",  # B5.3 efficiency, D-70 wording (d8d13a8d628ec17b)
        "run.json": "013702511c8c2d49",
    },
}
# Python 3.12 made sum() of floats compensated, so the estimate total differs in the last digit
# on 3.11 (0.4638 vs 0.46380000000000005); every line item is identical (L-27).
ESTIMATE_BEFORE = "7a7b7f9244cdd3c5" if sys.version_info < (3, 12) else "6a194982a4e4d495"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # never read the repo's real .env
    monkeypatch.delenv("EVALKIT_GIT_SHA", raising=False)
    rows = json.loads(MINI.read_text(encoding="utf-8"))
    monkeypatch.setattr(R, "load_pqa_l", lambda: parse_pqa_l(rows))
    (tmp_path / "evalkit.yaml").write_text(CONFIG, encoding="utf-8")
    cases = parse_pqa_l(rows)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "loc.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "id": c.id,
                    "question": c.question,
                    "context": list(c.context),
                    "label": c.reference_label,
                    "meta": {"grp": "a" if i % 2 else "b"},
                }
            )
            + "\n"
            for i, c in enumerate(cases)
        ),
        encoding="utf-8",
    )
    for name, extra in (("loc.yaml", []), ("loc_cite.yaml", ["citation_support"])):
        t = builtin_task("pubmedqa").model_dump()
        t.update({"name": "loc", "dataset": "data/loc.jsonl", "split": "local", "slices": ["grp"]})
        t["scorers"] = [*t["scorers"], *extra]
        (tmp_path / name).write_text(yaml.safe_dump(t, sort_keys=False), encoding="utf-8")
    return tmp_path


def execute(task: str, adapter, *, wait_s: float = 1.0, live: bool = False):
    plan = R.plan_run(
        Path("evalkit.yaml"), task=task, n=12, out=Path("runs"), store_dir=Path("store")
    )
    outcome = asyncio.run(
        R.execute(
            plan,
            {"anthropic": adapter, "openai": adapter},
            PRICES,
            available_providers={"anthropic", "openai"},
            wait_s=wait_s,
            live_judges=live,
            now=lambda: T0,
        )
    )
    return plan, outcome


def _fp(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@pytest.mark.golden
def test_tasks_without_the_scorer_are_byte_identical_to_before(project):
    assert "citation_support" in BENCHMARK_SCORERS
    assert resolve_task_scorers(R.load_task("loc_cite.yaml")[0]) == ()  # not a plugin lookup
    for task, want in BEFORE.items():
        plan, _outcome = execute(task, ScriptedBatchAdapter(full_reply))
        assert (plan.run_id, plan.config_hash) == (want["run_id"], want["config_hash"])
        for name in ("records.jsonl", "summary.json", "report.html", "run.json"):
            assert _fp(plan.run_dir / name) == want[name], (task, name)
        text = (plan.run_dir / "records.jsonl").read_text(encoding="utf-8") + (
            plan.run_dir / "report.html"
        ).read_text(encoding="utf-8")
        assert "citation_support" not in text and "Citation precision" not in text
        est = R.estimate(plan, PRICES, live_judges=False)
        assert hashlib.sha256(est.model_dump_json().encode()).hexdigest()[:16] == ESTIMATE_BEFORE


@pytest.mark.safety
def test_estimate_and_dry_run_include_citation_calls_only_when_listed(project):
    plain = R.plan_run(Path("evalkit.yaml"), task="loc.yaml", n=12, out=Path("runs"))
    cite = R.plan_run(Path("evalkit.yaml"), task="loc_cite.yaml", n=12, out=Path("runs"))
    for live in (False, True):
        a = R.estimate(plain, PRICES, live_judges=live)
        b = R.estimate(cite, PRICES, live_judges=live)
        extra = [ln for ln in b.lines if ln.node == "citation_judge"]
        assert [ln.model for ln in extra] == list(JUDGES)
        judge_calls = {ln.model: ln.calls for ln in a.lines if ln.node == "judge"}
        assert all(ln.calls == judge_calls[ln.model] and ln.usd > 0 for ln in extra)
        assert b.lines[: len(a.lines)] == a.lines
        assert math.isclose(b.total_usd, a.total_usd + sum(ln.usd for ln in extra))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(
            ["run", "evalkit.yaml", "--task", "loc_cite.yaml", "--n", "12", "--dry-run", "--json"]
        )
    assert code == 0
    nodes = [ln["node"] for ln in json.loads(buf.getvalue())["estimate"]["lines"]]
    assert nodes.count("citation_judge") == 2


def _expected_citation_calls(records: list[Record], cases: dict[str, Case]) -> int:
    n = 0
    for r in records:
        ctx = len(cases[r.case_id].context)
        n += sum(1 for c in r.claims if c.cited and all(1 <= x <= ctx for x in c.cited))
    return n * len(JUDGES)


@pytest.mark.parametrize("live", [False, True])
def test_rerun_that_adds_the_scorer_calls_only_the_citation_judge(project, live):
    fake = ScriptedBatchAdapter(full_reply)
    plain, _ = execute("loc.yaml", fake, live=live)
    before, batches = len(fake.requests), len(fake.submitted)
    plan, outcome = execute("loc_cite.yaml", fake, live=live)
    new = fake.requests[before:]
    records, cases = R.read_records(plan.run_dir), R.read_cases(plan.run_dir)
    assert new and {r.model for r in new} == {"citation_judge"}
    assert len(new) == _expected_citation_calls(records, cases)
    assert all(br.request.model == "citation_judge" for b in fake.submitted[batches:] for br in b)
    assert (fake.submitted[batches:] == []) is live
    assert outcome.exit_code in (0, 1) and outcome.summary.judging_status == "complete"
    # Answers, claims and claim verdicts are the ones already cached.
    old = R.read_records(plain.run_dir)
    assert [(r.completion, r.claims, r.verdicts, r.scores) for r in records] == [
        (r.completion, r.claims, r.verdicts, r.scores) for r in old
    ]
    # Rerunning the citation task is free.
    n = len(fake.requests)
    execute("loc_cite.yaml", fake, live=live)
    assert len(fake.requests) == n


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _hand(records: list[Record], judge: str, seed: int, n_res: int = 10_000):
    """An independent computation of both metrics for one model's records (sorted by case)."""
    num, den, hit = [], [], []
    for r in records:
        sentences: dict[int, list[CitationVerdict]] = {}
        for v in r.citation_support.get(judge, ()):
            sentences.setdefault(v.n, []).append(v)
        sup = {n for n, vs in sentences.items() if any(v.supports for v in vs)}
        aim = False
        for n, vs in sentences.items():
            tally = {role: sum(v.role == role for v in vs) for role in CITATION_ROLES}
            best = max(tally.values())
            for role in ("aim", "background", "method", "conclusion", "result"):
                if tally[role] == best:
                    break
            aim = aim or (role == "aim" and n not in sup)
        num.append(len(sup))
        den.append(len(sentences))
        hit.append(1 if (aim and sentences) else 0)
    eligible = [1 if d else 0 for d in den]
    rs = Resamples(len(records), n_res, seed=seed)

    def boot(a, b):
        draws = [
            sum(a[i] for i in row) / sum(b[i] for i in row)
            for row in rs.rows
            if sum(b[i] for i in row)
        ]
        return _quantile(draws, 0.025), _quantile(draws, 0.975)

    return (sum(num) / sum(den), *boot(num, den)), (sum(hit) / sum(eligible), *boot(hit, eligible))


def _close(iv: Interval, want: tuple[float, float, float]) -> bool:
    p, lo, hi = want
    return all(
        math.isclose(a, b, abs_tol=1e-12)
        for a, b in ((iv.point, p), (iv.lo, min(lo, p)), (iv.hi, max(hi, p)))
    )


@pytest.mark.contract
def test_metrics_reach_records_summary_slices_and_report_and_match_a_hand_computation(project):
    plan, outcome = execute("loc_cite.yaml", ScriptedBatchAdapter(full_reply))
    records = R.read_records(plan.run_dir)
    summary = RunSummary.model_validate_json(
        (plan.run_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary == outcome.summary and summary.judging_status == "complete"
    # A record names a judge only when that judge judged one of its citations (omitted when
    # empty, verifier B5.12): answers whose claims all failed the precheck carry no field.
    for r in records:
        judged = any(c.cited for c in r.claims)
        assert set(r.citation_support) == (set(JUDGES) if judged else set()), r.case_id
        assert all(r.citation_support[j] for j in r.citation_support)
    raw_lines = (plan.run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
    for line in raw_lines:
        raw = json.loads(line)
        assert "citation_support" not in raw or all(raw["citation_support"].values())
    for m in summary.models:
        rows = sorted(
            (r for r in records if r.completion.model == m.model), key=lambda r: r.case_id
        )
        assert set(m.citation_precision) == set(m.aim_citation_rate) == set(JUDGES)
        for j in JUDGES:
            precision, aim = _hand(rows, j, seed=7)
            assert _close(m.citation_precision[j], precision), (m.model, j)
            assert _close(m.aim_citation_rate[j], aim), (m.model, j)
    # Slices carry the same metrics over each slice's cases only.
    cases = R.read_cases(plan.run_dir)
    assert summary.slices
    for s in summary.slices:
        rows = sorted(
            (
                r
                for r in records
                if r.completion.model == s.model and cases[r.case_id].meta["grp"] == s.value
            ),
            key=lambda r: r.case_id,
        )
        for j in JUDGES:
            precision, aim = _hand(rows, j, seed=7)
            assert _close(s.citation_precision[j], precision)
            assert _close(s.aim_citation_rate[j], aim)
    html = (plan.run_dir / "report.html").read_text(encoding="utf-8")
    for j in JUDGES:
        assert f"Citation precision · {j}" in html and f"Aim-citation rate · {j}" in html
    # Re-rendering from the run folder alone gives the same bytes (rescoring is exact).
    before = (plan.run_dir / "summary.json").read_bytes(), html
    R.render(plan.run_dir)
    again = R.summarise(plan.run_dir, R.read_manifest(plan.run_dir), records, "complete")
    assert (
        again == summary and (plan.run_dir / "report.html").read_text(encoding="utf-8") == before[1]
    )


@pytest.mark.contract
def test_pending_citation_judging_exits_4_without_citation_figures(project):
    fake = ScriptedBatchAdapter(full_reply)
    execute("loc.yaml", fake)  # answers, claims and claim verdicts cached
    fake.pending_polls = 10**9  # citation batches stay open
    plan, outcome = execute("loc_cite.yaml", fake, wait_s=0)
    assert outcome.exit_code == R.EXIT_PENDING == 4
    s = outcome.summary
    assert s.judging_status == "pending" and s.finished_at is None
    assert all(not m.citation_precision and not m.aim_citation_rate for m in s.models)
    assert all(not x.citation_precision for x in s.slices)
    text = (plan.run_dir / "summary.json").read_text(encoding="utf-8") + (
        plan.run_dir / "records.jsonl"
    ).read_text(encoding="utf-8")
    assert "citation_precision" not in text and "citation_support" not in text
    assert {br.request.model for b in fake.submitted[-2:] for br in b} == {"citation_judge"}
    batches = len(fake.submitted)
    fake.pending_polls = 0
    plan, outcome = execute("loc_cite.yaml", fake)
    assert outcome.exit_code in (0, 1) and len(fake.submitted) == batches  # collected, not resent
    assert all(set(m.citation_precision) == set(JUDGES) for m in outcome.summary.models)


@pytest.mark.contract
def test_record_and_summary_contracts():
    claim = Claim(id="c:m:0", text="t", cited=(1, 2), extractor_version="x")
    rec = json.loads((FIX / "pqa-10808977.json").read_text(encoding="utf-8"))["record"]
    base = Record.model_validate(rec)
    assert "citation_support" not in json.loads(base.model_dump_json())  # omitted when empty
    cv = CitationVerdict(claim_id=base.claims[0].id, n=3, supports=False, role="aim")
    with_cs = Record.model_validate({**rec, "citation_support": {"j": [cv.model_dump()]}})
    assert json.loads(with_cs.model_dump_json())["citation_support"]["j"][0]["role"] == "aim"
    for bad in (
        {"claim_id": claim.id, "n": 3, "supports": False, "role": "aim"},  # not this record's
        {"claim_id": base.claims[0].id, "n": 4, "supports": False, "role": "aim"},  # not cited
    ):
        with pytest.raises(ValueError):
            Record.model_validate({**rec, "citation_support": {"j": [bad]}})
    with pytest.raises(ValueError, match="twice"):
        Record.model_validate({**rec, "citation_support": {"j": [cv.model_dump()] * 2}})
    with pytest.raises(ValueError):
        CitationVerdict(claim_id="c", n=1, supports="yes", role="aim")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CitationVerdict(claim_id="c", n=1, supports=True, role="finding")  # type: ignore[arg-type]


def _one_record_summary(record: Record, judges=JUDGES) -> RunSummary:
    return aggregate(
        [record],
        run_id=record.run_id,
        task=record.task,
        config_hash="x",
        models=(record.completion.model,),
        seed=7,
        family_of=lambda m: "other",
        selection=JudgeSelection(judges=judges, judge_mode="dual", n_available=2),
        judging=[JudgingResult(judge=j) for j in judges],
        started_at=T0,
        finished_at=T0,
        n_resamples=200,
    )


@pytest.mark.contract
def test_a_pending_summary_cannot_carry_citation_figures():
    rec = json.loads((FIX / "pqa-10808977.json").read_text(encoding="utf-8"))["record"]
    cv = {"claim_id": rec["claims"][0]["id"], "n": 3, "supports": False, "role": "aim"}
    record = Record.model_validate({**rec, "citation_support": {key: [cv] for key in JUDGES}})
    s = _one_record_summary(record)
    assert s.models[0].aim_citation_rate
    data = json.loads(s.model_dump_json())
    data.update(judging_status="pending", finished_at=None, comparisons=[])
    data["models"][0].update(headline={}, groundedness_mean={})
    data["self_preference"] = {}
    with pytest.raises(ValueError, match="per-judge"):
        RunSummary.model_validate(data)


def test_role_is_the_most_common_reading_with_ties_toward_aim():
    def cv(claim, n, supports, role):
        return CitationVerdict(claim_id=claim, n=n, supports=supports, role=role)

    # Sentence 1: result twice, aim once -> result. Sentence 2: one aim, one result -> aim.
    vs = [
        cv("a", 1, False, "result"),
        cv("b", 1, False, "result"),
        cv("c", 1, True, "aim"),
        cv("a", 2, False, "result"),
        cv("b", 2, False, "aim"),
    ]
    assert citation_counts(vs) == (1, 2, True)  # 1 supports (via c); 2 is a non-supporting aim
    tie = [cv("a", 4, False, "conclusion"), cv("b", 4, False, "result")]
    assert citation_counts(tie) == (0, 1, False)  # conclusion beats result, and is not aim
    assert citation_counts([cv("a", 5, True, "aim")]) == (1, 1, False)  # a supporting aim
    assert citation_counts([]) == (0, 0, False)


@pytest.mark.safety
def test_report_escapes_judge_names_in_citation_columns(project):
    _, outcome = execute("loc_cite.yaml", ScriptedBatchAdapter(full_reply))
    m = outcome.summary.models[0]
    iv = Interval(point=0.5, lo=0.4, hi=0.6)
    odd = m.model_copy(
        update={"citation_precision": {"<j&>": iv}, "aim_citation_rate": {"<j&>": iv}}
    )
    html = _model_table(outcome.summary.model_copy(update={"models": (odd,)}))
    assert "Citation precision · &lt;j&amp;&gt;" in html and "<j&>" not in html


# ---- CIT-03 the asking project's cases -----------------------------------------------------------

REPLIES = json.loads((FIX / "replies.json").read_text(encoding="utf-8"))


def _recorded(claims: dict[str, Claim]):
    by_text = {c.text: cid for cid, c in claims.items()}

    def answer(req):
        text = req.prompt.split("Claim: ", 1)[1].split("\n", 1)[0]
        return REPLIES[by_text[text]][req.model_id]

    return answer


def _judge_fixture(tmp_path, case: Case, claim: Claim) -> dict[str, CitationResult]:
    fake = ScriptedBatchAdapter(_recorded({claim.id: claim}))
    ex = Executor(Store(tmp_path))
    ads = {"anthropic": fake, "openai": fake}
    out = {
        j: asyncio.run(
            judge_citations(ex, [(case, claim)], j, CFG, ads, PRICES, run_id="r", max_wait_s=1)
        )
        for j in JUDGES
    }
    assert all(len(b) == 1 for b in fake.submitted) and len(fake.submitted) == 2
    shown = fake.submitted[0][0].request.prompt
    for n in claim.cited:
        assert f"[{n}] {case.context[n - 1]}" in shown
    return out


def test_gemma_pqa_10808977_cites_its_aim_sentence(tmp_path):
    fix = json.loads((FIX / "pqa-10808977.json").read_text(encoding="utf-8"))
    case = Case.model_validate(fix["case"])
    record = Record.model_validate(fix["record"])
    claim = record.claims[0]
    assert case.question == "Can tailored interventions increase mammography use among HMO women?"
    assert claim.text.startswith("Telephone counseling is more effective") and claim.cited == (3,)
    results = _judge_fixture(tmp_path, case, claim)
    for j in JUDGES:
        (v,) = results[j].verdicts[claim.id]
        assert (v.n, v.role, v.supports) == (3, "aim", False)
        assert citation_counts(results[j].verdicts[claim.id]) == (0, 1, True)
    scored = Record.model_validate(
        {
            **fix["record"],
            "citation_support": {
                j: [v.model_dump() for v in results[j].verdicts[claim.id]] for j in JUDGES
            },
        }
    )
    m = _one_record_summary(scored).models[0]
    for j in JUDGES:
        assert m.aim_citation_rate[j].point == 1.0  # the answer counts as an aim citation
        assert m.citation_precision[j].point == 0.0


def test_aim_beside_result_counts_although_the_claim_is_supported(tmp_path):
    fix = json.loads((FIX / "aim_beside_result.json").read_text(encoding="utf-8"))
    gemma = json.loads((FIX / "pqa-10808977.json").read_text(encoding="utf-8"))
    case = Case.model_validate(gemma["case"])
    claim = Claim.model_validate(fix["claim"])
    results = _judge_fixture(tmp_path, case, claim)
    rec = dict(gemma["record"])
    rec["claims"] = [claim.model_dump()]
    rec["verdicts"] = [
        {
            **fix["claim_verdict"],
            "claim_id": claim.id,
            "judge": j,
            "judge_prompt_hash": JUDGE_PROMPT_HASH,
        }
        for j in JUDGES
    ]
    rec["scores"] = {
        **rec["scores"],
        "groundedness": dict.fromkeys(JUDGES, 1.0),
        "fully_grounded": dict.fromkeys(JUDGES, True),
        "correct_and_grounded": dict.fromkeys(JUDGES, True),
    }
    rec["citation_support"] = {
        j: [v.model_dump() for v in results[j].verdicts[claim.id]] for j in JUDGES
    }
    record = Record.model_validate(rec)
    assert all(v.verdict == "supported" for v in record.verdicts)  # the claim itself holds
    for j in JUDGES:
        by_n = {v.n: v for v in record.citation_support[j]}
        assert (by_n[3].role, by_n[3].supports) == ("aim", False)
        assert (by_n[8].role, by_n[8].supports) == ("result", True)
    m = _one_record_summary(record).models[0]
    for j in JUDGES:
        assert m.citation_precision[j].point == 0.5  # 1 of 2 cited sentences supports
        assert m.aim_citation_rate[j].point == 1.0  # yet the answer cites a bare aim sentence
