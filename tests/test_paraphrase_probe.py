"""B5.8: the paraphrase contamination probe (PAR-01 set and builder, PAR-02 report).

Every model call here goes to a scripted adapter: no network, no spend. The vendored set is
checked against the real PQA-L when it is cached on this machine; everything else runs on a
synthetic PQA-L-shaped population.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalkit import cli
from evalkit import paraphrase as P
from evalkit import run as R
from evalkit.adapters import CompletionRequest
from evalkit.cost import load_prices
from evalkit.runtime import BudgetExceeded, MemoryStore, Store
from evalkit.sampling import stratified_sample
from evalkit.schemas import SCHEMA_VERSION, Answer, Case, Completion, Record
from evalkit.tasks import TASKS_DIR, builtin_task
from tests.pipeline.fakes import ScriptedAdapter

PRICES = load_prices()


# --- synthetic population and scripted models ---------------------------------------------


def population(yes: int = 33, no: int = 20, maybe: int = 7) -> list[Case]:
    out, i = [], 0
    for label, k in (("yes", yes), ("no", no), ("maybe", maybe)):
        for _ in range(k):
            i += 1
            out.append(
                Case(
                    id=f"pqa-{10_000 + i}",
                    question=f"Does treatment {i} change outcome {i} in adults?",
                    context=(f"Study {i} enrolled adults.", f"Outcome {i} was measured."),
                    reference_label=label,
                    source="pubmedqa/pqa_l",
                )
            )
    return out


def _question(prompt: str) -> str:
    return prompt.rsplit("Question: ", 1)[1].strip()


def _original(prompt: str) -> str:
    return prompt.split("\nOriginal: ", 1)[1].split("\nRewritten: ", 1)[0].strip()


class Models:
    """A scripted paraphraser and checker. `reject` names original questions the checker
    calls DIFFERENT; `log` records every call (and echo lines) in order."""

    def __init__(self, reject: set[str] = frozenset(), log: list[str] | None = None) -> None:  # type: ignore[assignment]
        self.reject = set(reject)
        self.log = log if log is not None else []
        self.openai = ScriptedAdapter(self.reply)
        self.anthropic = ScriptedAdapter(self.reply)

    def reply(self, req: CompletionRequest) -> str:
        self.log.append(f"call:{req.model_id}")
        if req.model_id == P.PARAPHRASE_MODEL:
            q = _question(req.prompt)
            return f"Rewritten: {q.rstrip('?')} (reworded)?\nignored second line"
        orig = _original(req.prompt)
        if orig in self.reject:
            return "DIFFERENT\nThe rewrite changes the question."
        return "SAME\nBoth ask the same thing."

    @property
    def adapters(self):
        return {"openai": self.openai, "anthropic": self.anthropic}

    @property
    def calls(self) -> int:
        return len(self.openai.requests) + len(self.anthropic.requests)


def build(cases, models: Models, store=None, **kw):
    kw.setdefault("n", 10)
    kw.setdefault("echo", models.log.append)
    return asyncio.run(P.build_probe(cases, models.adapters, PRICES, store or MemoryStore(), **kw))


def synthetic_set(tmp_path: Path, cases, n: int = 10, reject: set[str] = frozenset()):  # type: ignore[assignment]
    res = build(cases, Models(reject=reject), n=n)
    out = tmp_path / "set"
    P.write_probe(out, res)
    return P.load_set(out)


# --- PAR-01: the vendored set ---------------------------------------------------------------


def test_vendored_files_are_the_gemma_bytes_named_in_the_provenance():
    """Golden: the vendored files are byte-for-byte gemma-grounded-health c7d70d2."""
    prov = P.load_provenance()
    assert prov.source_repo == "github.com/biplob819/gemma-grounded-health"
    assert prov.source_commit == "c7d70d2317f9e8cc2bcbe194edfe5443205d0906"
    expected = {
        "pubmedqa_paraphrase.jsonl": (
            "8300d30a349b7b268dc2967bb9ab5d5d7bbbe0b7861666c81bfe05725b5d4707",
            100,
        ),
        "pubmedqa_paraphrase_rejects.jsonl": (
            "0279ecca32c8099e98d5f7796d15bfd4ce88f3ede0e249a817d6783eb8630963",
            1,
        ),
    }
    for name, (sha, rows) in expected.items():
        blob = (TASKS_DIR / name).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == sha == prov.files[name].sha256
        assert len(blob.splitlines()) == rows == prov.files[name].rows
    assert prov.method == "DATA_CARD DAT-06"
    assert (prov.paraphrase_model, prov.check_model) == ("gpt-6.1-terra", "claude-opus-5-5")
    assert prov.spot_check.status == "pending"
    assert "MIT" in prov.licence and "PubMedQA" in prov.licence


def test_vendored_set_keeps_every_field_and_the_dat06_label_mix():
    probe = P.load_set()
    assert len(probe.kept) == 100 and len(probe.rejected) == 1
    assert Counter(r.reference_label for r in probe.kept) == {"yes": 55, "no": 34, "maybe": 11}
    assert len({r.pubid for r in probe.kept}) == 100
    assert all(r.pubid.startswith("pqa-") and r.pubid[4:].isdigit() for r in probe.kept)
    assert all(r.check_verdict == "same" for r in probe.kept)
    assert probe.rejected[0].check_verdict == "different" and probe.rejected[0].check_reason
    # Same serialisation as the source: reading and writing gives back the exact bytes.
    assert P.jsonl(probe.kept) == (TASKS_DIR / "pubmedqa_paraphrase.jsonl").read_text("utf-8")
    assert P.jsonl(probe.rejected) == (TASKS_DIR / "pubmedqa_paraphrase_rejects.jsonl").read_text(
        "utf-8"
    )


def test_recorded_prompt_hashes_are_the_builder_prompts():
    assert P.prompt_hash(P.PARAPHRASE_PROMPT) == "1bb383c050490b51"
    assert P.prompt_hash(P.CHECK_PROMPT) == "2331517ce31052a8"
    probe = P.load_set()
    for r in (*probe.kept, *probe.rejected):
        assert r.paraphrase_prompt_hash == P.prompt_hash(P.PARAPHRASE_PROMPT)
        assert r.check_prompt_hash == P.prompt_hash(P.CHECK_PROMPT)


def test_vendored_set_matches_the_dat06_sample_on_the_real_pqa_l_when_cached():
    from evalkit.datasets.pubmedqa import cache_dir, load_pqa_l

    if not (cache_dir() / "pubmedqa_pqa_l.json").is_file():
        pytest.skip("PQA-L not cached on this machine")
    cases = load_pqa_l()
    probe = P.load_set()
    assert P.probe_problems(cases, probe) == []
    first = stratified_sample(cases, 100, 7)
    assert first.label_mix == {"maybe": 11, "no": 34, "yes": 55}
    # One rejected (a maybe) was replaced by the next maybe in the seeded order.
    assert {r.pubid for r in probe.kept} ^ {c.id for c in first.cases} == {
        probe.rejected[0].pubid,
        probe.kept[-1].pubid,
    }
    para = P.probe_cases(cases, "paraphrase")
    orig = P.probe_cases(cases, "original")
    by_id = {c.id: c for c in cases}
    assert [c.id for c in para] == [c.id for c in orig] == [r.pubid for r in probe.kept]
    for p, o in zip(para, orig, strict=True):
        assert o == by_id[o.id]  # original wording is PQA-L's case, unchanged
        assert (p.context, p.reference_label) == (o.context, o.reference_label)
        assert p.question != o.question


def test_probe_cases_pair_exactly_on_pqa_ids(tmp_path, monkeypatch):
    cases = population()
    probe = synthetic_set(tmp_path, cases)
    para = P.probe_cases(cases, "paraphrase", probe)
    orig = P.probe_cases(cases, "original", probe)
    by_id = {c.id: c for c in cases}
    assert [c.id for c in para] == [c.id for c in orig] == [r.pubid for r in probe.kept]
    for p, o, r in zip(para, orig, probe.kept, strict=True):
        assert o == by_id[r.pubid]
        assert p.question == r.paraphrase and o.question == r.original
        assert (p.context, p.reference_label) == (o.context, o.reference_label)
    with pytest.raises(ValueError, match="wording"):
        P.probe_cases(cases, "reworded", probe)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("unknown_pubid", "not in PQA-L"),
        ("changed_original", "original question"),
        ("changed_label", "label"),
        ("dropped_reject", "does not follow"),
        ("swapped_order", "does not follow"),
        ("wrong_hash", "prompt hash"),
    ],
)
def test_probe_problems_catches_a_set_that_left_the_method(tmp_path, damage, message):
    cases = population()
    first = stratified_sample(cases, 10, 7).cases
    probe = synthetic_set(tmp_path, cases, reject={first[0].question, first[5].question})
    assert P.probe_problems(cases, probe) == [] and len(probe.rejected) == 2
    kept, rejected = list(probe.kept), list(probe.rejected)
    if damage == "unknown_pubid":
        kept[0] = kept[0].model_copy(update={"pubid": "pqa-1"})
    elif damage == "changed_original":
        kept[1] = kept[1].model_copy(update={"original": "Something else?"})
    elif damage == "changed_label":
        kept[2] = kept[2].model_copy(update={"reference_label": "maybe"})
    elif damage == "dropped_reject":
        rejected = rejected[:-1]
    elif damage == "swapped_order":
        kept[0], kept[1] = kept[1], kept[0]
    else:
        kept[3] = kept[3].model_copy(update={"paraphrase_prompt_hash": "0" * 16})
    bad = probe.model_copy(update={"kept": tuple(kept), "rejected": tuple(rejected)})
    problems = P.probe_problems(cases, bad)
    assert problems and any(message in p for p in problems), problems
    with pytest.raises(P.ProbeError):
        P.probe_cases(cases, "paraphrase", bad)


@pytest.mark.safety
def test_a_tampered_vendored_file_is_refused(tmp_path):
    for name in P.SET_FILES:
        (tmp_path / name).write_bytes((TASKS_DIR / name).read_bytes())
    assert len(P.load_set(tmp_path).kept) == 100
    path = tmp_path / "pubmedqa_paraphrase.jsonl"
    text = path.read_text("utf-8")
    assert "Does parity" in text
    path.write_text(text.replace("Does parity", "Does gravidity", 1), encoding="utf-8")
    with pytest.raises(P.ProbeError, match="sha256"):
        P.load_set(tmp_path)


def test_builtin_tasks_differ_from_pubmedqa_only_in_wording_source():
    base = builtin_task("pubmedqa")
    for name, dataset in (
        ("pubmedqa-paraphrase", "evalkit/pubmedqa-paraphrase"),
        ("pubmedqa-paraphrase-original", "evalkit/pubmedqa-paraphrase-original"),
    ):
        t = builtin_task(name)
        assert (t.name, t.dataset) == (name, dataset)
        assert (t.prompt_template, t.labels, t.scorers, t.thresholds) == (
            base.prompt_template,
            base.labels,
            base.scorers,
            base.thresholds,
        )
        assert P.DATASETS[dataset] in ("original", "paraphrase")


@pytest.mark.contract
def test_dry_run_of_both_probe_tasks_plans_the_same_pubids(tmp_path, monkeypatch, capsys):
    cases = population()
    probe = synthetic_set(tmp_path, cases)
    monkeypatch.setattr(R, "load_pqa_l", lambda: cases)
    monkeypatch.setattr(P, "SET_DIR", tmp_path / "set")
    cfg = tmp_path / "evalkit.yaml"
    cfg.write_text(
        "models: [claude-sonnet-5-5, gpt-6.1-sol]\njudges: [claude-opus-5-5]\n"
        "extractor: claude-haiku-5-5\nseed: 7\n",
        encoding="utf-8",
    )
    ids = {}
    for task in ("pubmedqa-paraphrase", "pubmedqa-paraphrase-original"):
        plan = R.plan_run(cfg, task=task, n=10, out=tmp_path / "runs")
        ids[task] = sorted(c.id for c in plan.cases)
        code = cli.main(["run", str(cfg), "--task", task, "--n", "10", "--dry-run", "--json"])
        assert code == 0
        assert json.loads(capsys.readouterr().out)["dry_run"] is True
    assert ids["pubmedqa-paraphrase"] == ids["pubmedqa-paraphrase-original"]
    assert ids["pubmedqa-paraphrase"] == sorted(r.pubid for r in probe.kept)


def test_a_broken_set_stops_the_run_before_any_call(tmp_path, monkeypatch):
    cases = population()
    probe = synthetic_set(tmp_path, cases)
    gone = probe.kept[0].pubid
    monkeypatch.setattr(R, "load_pqa_l", lambda: [c for c in cases if c.id != gone])
    monkeypatch.setattr(P, "SET_DIR", tmp_path / "set")
    with pytest.raises(R.RunError, match="not in PQA-L"):
        R.load_cases(builtin_task("pubmedqa-paraphrase"), None)


# --- PAR-01: the builder ----------------------------------------------------------------------


def test_builder_follows_the_method_with_replacements_from_the_same_label():
    cases = population()
    first = stratified_sample(cases, 10, 7).cases
    rejected_q = {first[0].question, first[3].question}
    models = Models(reject=rejected_q)
    res = build(cases, models, n=10)
    assert len(res.kept) == 10 and len(res.rejected) == 2
    assert Counter(r.reference_label for r in res.kept) == Counter(c.reference_label for c in first)
    assert {r.original for r in res.rejected} == rejected_q
    # Kept: the first draw's order, replacements after it, each from its label's seeded order.
    survivors = [c.id for c in first if c.question not in rejected_q]
    assert [r.pubid for r in res.kept][:8] == survivors
    order = stratified_sample(cases, len(cases), 7).cases
    drawn = {c.id for c in first}
    spare = {
        lab: [c.id for c in order if c.reference_label == lab and c.id not in drawn]
        for lab in ("yes", "no", "maybe")
    }
    for rej, rep in zip(res.rejected, res.kept[8:], strict=True):
        assert rep.pubid == spare[rej.reference_label].pop(0)
        assert rep.reference_label == rej.reference_label
    r = res.kept[0]
    assert r.paraphrase == f"{r.original.rstrip('?')} (reworded)?"  # first line, label stripped
    assert (r.paraphrase_model, r.check_model) == (P.PARAPHRASE_MODEL, P.CHECK_MODEL)
    assert r.paraphrase_prompt_hash == "1bb383c050490b51"
    assert r.check_prompt_hash == "2331517ce31052a8"
    assert P.probe_problems(cases, P.ProbeSet.of(res)) == []


def test_builder_prints_its_estimate_before_the_first_call():
    log: list[str] = []
    models = Models(log=log)
    res = build(population(), models, n=10, max_cost_usd=5.0)
    assert log[0].startswith("paraphrase probe:") and "estimate $" in log[0]
    assert "cap $5.00" in log[0]
    assert all(not line.startswith("call:") for line in log[:1])
    assert log.index(next(x for x in log if x.startswith("call:"))) > 0
    assert res.estimate_usd == pytest.approx(
        P.estimate_build(population(), PRICES, n=10, seed=7), rel=1e-12
    )
    assert res.estimate_usd >= res.spent_usd > 0


def test_estimate_is_an_upper_bound_from_the_price_table():
    cases = population()
    est = P.estimate_build(cases, PRICES, n=10, seed=7)
    first = stratified_sample(cases, 10, 7).cases
    node = P.ProbeCall({}, PRICES)
    ctx = P.context()
    by_hand = sum(
        node.estimate_cost(P.paraphrase_input(c, P.PARAPHRASE_MODEL), ctx)
        + node.estimate_cost(P.check_input(c, c.question, P.CHECK_MODEL), ctx)
        for c in first
    )
    assert est == pytest.approx(by_hand) and est > 0


def test_unparseable_or_identical_replies_are_rejected_not_kept():
    cases = population()
    first = stratified_sample(cases, 10, 7).cases

    class Odd(Models):
        def reply(self, req):
            if req.model_id == P.PARAPHRASE_MODEL and _question(req.prompt) == first[1].question:
                return first[1].question.upper()  # same words: not a paraphrase
            if req.model_id == P.CHECK_MODEL and _original(req.prompt) == first[2].question:
                return "Probably the same"
            return super().reply(req)

    res = build(cases, Odd(), n=10)
    assert {r.pubid for r in res.rejected} == {first[1].id, first[2].id}
    assert (
        "SAME or DIFFERENT" in next(r for r in res.rejected if r.pubid == first[2].id).check_reason
    )


def test_builder_is_deterministic_and_replays_from_the_store(tmp_path):
    cases = population()
    store = Store(tmp_path / "store")
    gone = {stratified_sample(cases, 10, 7).cases[2].question}
    a = build(cases, Models(reject=gone), store=store, n=10)
    assert len(a.rejected) == 1
    again = Models(reject=gone)
    b = build(cases, again, store=store, n=10)
    assert again.calls == 0 and b.spent_usd == 0 and b.cache_hits > 0
    assert (a.kept, a.rejected) == (b.kept, b.rejected)
    fresh = build(cases, Models(reject=gone), n=10)  # new store, same replies
    assert (fresh.kept, fresh.rejected) == (a.kept, a.rejected)
    o1, o2 = tmp_path / "o1", tmp_path / "o2"
    P.write_probe(o1, a)
    P.write_probe(o2, fresh)
    for name in P.SET_FILES[:2]:
        assert (o1 / name).read_bytes() == (o2 / name).read_bytes()


def test_extending_reuses_cached_questions(tmp_path):
    cases = population()
    store = Store(tmp_path / "store")
    small = build(cases, Models(), store=store, n=10)
    models = Models()
    big = build(cases, models, store=store, n=20)
    assert len(big.kept) == 20
    reused = {r.pubid for r in small.kept} & {r.pubid for r in big.kept}
    assert models.calls == 2 * (20 - len(reused))


@pytest.mark.safety
def test_builder_stops_at_the_cost_cap_and_resumes(tmp_path):
    cases = population()
    store = Store(tmp_path / "store")
    models = Models()
    with pytest.raises(BudgetExceeded):
        build(cases, models, store=store, n=10, max_cost_usd=0.02)
    spent_calls = models.calls
    assert 0 < spent_calls < 20
    resumed = Models()
    res = build(cases, resumed, store=store, n=10, max_cost_usd=10.0)
    assert len(res.kept) == 10 and resumed.calls == 20 - spent_calls


@pytest.mark.safety
def test_write_probe_never_overwrites_the_vendored_set(tmp_path):
    res = build(population(), Models(), n=10)
    with pytest.raises(P.ProbeError, match="vendored"):
        P.write_probe(TASKS_DIR, res)
    prov = P.write_probe(tmp_path / "out", res)
    assert prov.spot_check.status == "pending" and prov.source_commit is None
    assert prov.paraphrase_prompt_hash == "1bb383c050490b51"
    loaded = P.load_set(tmp_path / "out")
    assert loaded.kept == tuple(res.kept) and loaded.provenance == prov


def test_module_cli_build_dry_run_prints_the_estimate_and_makes_no_call(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(P, "load_pqa_l", population)

    def no_adapters(*a, **k):
        raise AssertionError("a dry run must not build adapters")

    monkeypatch.setattr(P, "_live_adapters", no_adapters)
    code = P.main(["build", "--out", str(tmp_path / "o"), "--n", "10", "--dry-run", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["estimate_usd"] > 0 and out["n"] == 10
    assert not (tmp_path / "o").exists()


def test_module_cli_build_writes_a_set_with_fake_adapters(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(P, "load_pqa_l", population)
    models = Models()
    monkeypatch.setattr(P, "_live_adapters", lambda providers, prices: models.adapters)
    argv = ["build", "--out", str(tmp_path / "o"), "--n", "10", "--store", str(tmp_path / "s")]
    assert P.main(argv) == 0
    err = capsys.readouterr().err
    assert "estimate $" in err
    assert len(P.load_set(tmp_path / "o").kept) == 10
    assert P.main([*argv[:-2], "--store", str(tmp_path / "s"), "--max-cost", "0"]) == 0  # cached


def test_module_cli_verify(tmp_path, monkeypatch, capsys):
    cases = population()
    synthetic_set(tmp_path, cases)
    monkeypatch.setattr(P, "load_pqa_l", lambda: cases)
    monkeypatch.setattr(P, "SET_DIR", tmp_path / "set")
    assert P.main(["verify"]) == 0
    assert "matches DAT-06" in capsys.readouterr().out
    gone = P.load_set().kept[0].pubid
    monkeypatch.setattr(P, "load_pqa_l", lambda: [c for c in cases if c.id != gone])
    assert P.main(["verify"]) == 2


# --- PAR-02: the contamination report --------------------------------------------------------


def _completion(model: str, label: str | None) -> Completion:
    return Completion(
        model=model,
        model_snapshot=model,
        params={"effort": "low"},
        raw_text="{}",
        answer=None if label is None else Answer(label=label, answer="a", citations=(1,)),
        error="schema" if label is None else None,
        tokens_in=10,
        tokens_out=5,
        latency_ms=10,
        cost_usd=0.0,
    )


def write_run(
    folder: Path, run_id: str, cases: list[Case], answers: Mapping[str, Mapping[str, str | None]]
) -> Path:
    """A run folder in EvalKit's format: run.json, cases.jsonl and records.jsonl (unscored)."""
    folder.mkdir(parents=True)
    (folder / "run.json").write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
    (folder / "cases.jsonl").write_text(
        "".join(c.model_dump_json() + "\n" for c in cases), encoding="utf-8"
    )
    lines = []
    for model, by_case in answers.items():
        for cid, label in by_case.items():
            rec = Record(
                schema_version=SCHEMA_VERSION,
                run_id=run_id,
                task="t",
                case_id=cid,
                timestamp=datetime(2026, 11, 17, tzinfo=UTC),
                completion=_completion(model, label),
                claims=(),
                verdicts=(),
                scores=None,
                seed=7,
                git_sha="",
                prompt_hash="p",
            )
            lines.append(rec.model_dump_json() + "\n")
    (folder / "records.jsonl").write_text("".join(lines), encoding="utf-8")
    return folder


def wrong(label: str) -> str:
    return {"yes": "no", "no": "yes", "maybe": "yes"}[label]


def pop40() -> list[Case]:
    return population(66, 40, 14)


@pytest.fixture
def probe_runs(tmp_path):
    """A 40-question synthetic probe; model A answers every original right and 20 paraphrases
    wrong (contaminated); model B scores the same on both wordings."""
    cases = pop40()
    probe = synthetic_set(tmp_path, cases, n=40)
    orig = P.probe_cases(cases, "original", probe)
    para = P.probe_cases(cases, "paraphrase", probe)
    gold = {c.id: c.reference_label or "" for c in orig}
    ids = [c.id for c in orig]
    a_orig = dict(gold)
    a_para = {cid: (wrong(gold[cid]) if i < 20 else gold[cid]) for i, cid in enumerate(ids)}
    b = {cid: (gold[cid] if i % 2 else wrong(gold[cid])) for i, cid in enumerate(ids)}
    o = write_run(tmp_path / "orig", "r-orig", orig, {"model-a": a_orig, "model-b": b})
    p = write_run(tmp_path / "para", "r-para", para, {"model-a": a_para, "model-b": b})
    return probe, o, p, ids, gold


def test_report_gives_paired_accuracy_per_model_and_flags_a_drop(probe_runs):
    probe, o, p, _ids, _gold = probe_runs
    rep = P.contamination_report(o, p, probe=probe)
    by = {m.model: m for m in rep.models}
    a, b = by["model-a"], by["model-b"]
    assert (a.n, b.n, rep.n_paired, rep.n_probe) == (40, 40, 40, 40)
    assert (a.accuracy_original.point, a.accuracy_paraphrase.point) == (1.0, 0.5)
    assert a.drop.point == pytest.approx(0.5) and a.drop.lo > 0
    assert a.flagged and a.direction == "drop"
    assert b.drop.point == 0 and not b.flagged and b.direction == "none"
    assert (rep.n_resamples, rep.seed) == (10_000, 7)
    assert (rep.original_run_id, rep.paraphrase_run_id) == ("r-orig", "r-para")


def test_report_matches_evalkit_paired_bootstrap_exactly(probe_runs):
    """Golden: the report's interval is stats.paired on the same 10,000 seed-7 resamples."""
    from evalkit.stats import Resamples, interval, paired

    probe, o, p, ids, gold = probe_runs
    rep = P.contamination_report(o, p, probe=probe)
    a = next(m for m in rep.models if m.model == "model-a")
    order = sorted(ids)
    pos = {cid: i for i, cid in enumerate(ids)}
    orig = [1.0] * 40
    para = [0.0 if pos[c] < 20 else 1.0 for c in order]
    rs = Resamples(40, 10_000, seed=7)
    want, pv = paired(orig, para, rs)
    assert a.drop == want and a.p_value == pv
    assert a.accuracy_paraphrase == interval(para, rs)
    del gold


def test_report_is_reproducible(probe_runs):
    probe, o, p, _ids, _gold = probe_runs
    one = P.contamination_report(o, p, probe=probe)
    two = P.contamination_report(o, p, probe=probe)
    assert one.model_dump_json() == two.model_dump_json()


def test_report_flags_a_rise_too(probe_runs, tmp_path):
    probe, o, _p, ids, gold = probe_runs
    para = P.probe_cases(pop40(), "paraphrase", probe)
    orig = P.probe_cases(pop40(), "original", probe)
    worse = {cid: (wrong(gold[cid]) if i < 20 else gold[cid]) for i, cid in enumerate(ids)}
    o2 = write_run(tmp_path / "o2", "r-o2", orig, {"model-a": worse})
    p2 = write_run(tmp_path / "p2", "r-p2", para, {"model-a": dict(gold)})
    a = P.contamination_report(o2, p2, probe=probe).models[0]
    assert a.flagged and a.direction == "rise" and a.drop.hi < 0
    del o


def test_report_pairs_only_shared_pubids_and_models(probe_runs, tmp_path):
    probe, o, _p, ids, gold = probe_runs
    para = [c for c in P.probe_cases(pop40(), "paraphrase", probe) if c.id in ids[:30]]
    p2 = write_run(
        tmp_path / "para2",
        "r-para2",
        para,
        {"model-a": {c.id: gold[c.id] for c in para}, "model-c": {c.id: "yes" for c in para}},
    )
    rep = P.contamination_report(o, p2, probe=probe)
    assert [m.model for m in rep.models] == ["model-a"]
    assert rep.models[0].n == 30 and rep.n_paired == 30
    assert rep.unpaired_models == ("model-b", "model-c")
    assert "30 of 40" in P.report_text(rep)


def test_report_refuses_runs_with_the_wrong_wording(probe_runs, tmp_path):
    probe, o, p, _ids, _gold = probe_runs
    with pytest.raises(P.ProbeError, match="wording"):
        P.contamination_report(p, o, probe=probe)  # swapped
    with pytest.raises(P.ProbeError, match="no probe"):
        empty = write_run(tmp_path / "e", "r-e", population()[:2], {"model-a": {}})
        P.contamination_report(empty, p, probe=probe)


@pytest.mark.safety
def test_report_is_provisional_while_the_spot_check_is_pending(probe_runs):
    probe, o, p, _ids, _gold = probe_runs
    assert probe.provenance.spot_check.status == "pending"
    rep = P.contamination_report(o, p, probe=probe)
    assert rep.status == "provisional" and rep.spot_check == "pending"
    text = P.report_text(rep)
    assert text.startswith("PROVISIONAL") and "spot-check pending" in text
    assert "final" not in text.lower().replace("not final", "")
    with pytest.raises(P.ProbeError, match="spot-check"):
        P.require_final(rep)
    passed = probe.model_copy(
        update={
            "provenance": probe.provenance.model_copy(
                update={
                    "spot_check": probe.provenance.spot_check.model_copy(
                        update={"status": "passed", "by": "owner", "on": "2026-11-18"}
                    )
                }
            )
        }
    )
    final = P.contamination_report(o, p, probe=passed)
    assert final.status == "final" and P.report_text(final).startswith("FINAL")
    P.require_final(final)
    failed = passed.model_copy(
        update={
            "provenance": passed.provenance.model_copy(
                update={
                    "spot_check": passed.provenance.spot_check.model_copy(
                        update={"status": "failed"}
                    )
                }
            )
        }
    )
    with pytest.raises(P.ProbeError, match="failed"):
        P.contamination_report(o, p, probe=failed)


@pytest.mark.contract
def test_cli_report_paraphrase_writes_contamination_json(probe_runs, monkeypatch, capsys):
    probe, o, p, _ids, _gold = probe_runs
    monkeypatch.setattr(P, "SET_DIR", Path(probe.folder))
    code = cli.main(["report", "--paraphrase", str(o), str(p), "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "provisional" and out["spot_check"] == "pending"
    assert out["published"] is False
    saved = json.loads((p / "contamination.json").read_text("utf-8"))
    assert saved == out["contamination"]
    assert {m["model"] for m in saved["models"]} == {"model-a", "model-b"}
    code = cli.main(["report", "--paraphrase", str(o), str(p)])
    assert code == 0 and capsys.readouterr().out.startswith("PROVISIONAL")


@pytest.mark.safety
def test_cli_report_paraphrase_refuses_to_publish_while_pending(probe_runs, monkeypatch, capsys):
    probe, o, p, _ids, _gold = probe_runs
    monkeypatch.setattr(P, "SET_DIR", Path(probe.folder))
    code = cli.main(["report", "--paraphrase", str(o), str(p), "--publish"])
    assert code == 2
    assert "spot-check" in capsys.readouterr().err
    assert cli.main(["report", "runs/x", "--paraphrase", str(o), str(p)]) == 2
