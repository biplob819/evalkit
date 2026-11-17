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
from evalkit.adapters import CompletionRequest, RawCompletion
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

    def reply(self, req: CompletionRequest) -> str | RawCompletion:
        self.log.append(f"call:{req.model_id}")
        if req.model_id == P.PARAPHRASE_MODEL:
            q = _question(req.prompt)
            return f"{q.rstrip('?')} (reworded)?\nignored second line"
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
    assert r.paraphrase == f"{r.original.rstrip('?')} (reworded)?"  # the first line, verbatim
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


def _reservations(models: Models) -> float:
    """What the executor reserved for every call the scripted adapters saw (its spend cap is
    held against these), rebuilt from the requests."""
    node = P.ProbeCall({}, PRICES)
    total = 0.0
    for req in (*models.openai.requests, *models.anthropic.requests):
        para = req.model_id == P.PARAPHRASE_MODEL
        inp = P.ProbeCallInput(
            step="paraphrase" if para else "check",
            model_id=req.model_id,
            provider="openai" if para else "anthropic",
            prompt=req.prompt,
            effort=str(req.params["effort"]),
            max_output_tokens=req.max_output_tokens,
        )
        total += node.estimate_cost(inp, P.context())
    return total


LONGEST = 3 * P.PARAPHRASE_MAX_TOKENS - 3  # ASCII characters estimated at the token limit


def longest_paraphrase(question: str) -> str:
    return (f"Reworded: {question} " + "x" * LONGEST)[:LONGEST]


class Worst(Models):
    """The worst case the estimate must bound: every paraphrase as long as the builder accepts
    (estimated at exactly PARAPHRASE_MAX_TOKENS tokens) and `reject` questions rejected."""

    def reply(self, req):
        if req.model_id == P.PARAPHRASE_MODEL:
            self.log.append(f"call:{req.model_id}")
            return longest_paraphrase(_question(req.prompt))
        return super().reply(req)


def test_estimate_bounds_the_spend_with_longest_paraphrases_and_rejects_up_to_the_cap():
    """Item 5: the estimate is a strict upper bound on what the executor can reserve (and so
    spend), sizing each check with the longest accepted paraphrase and allowing a replacement
    draw for every reject up to the reject cap."""
    from evalkit.cost import estimate_tokens

    cases = population()
    first = stratified_sample(cases, 10, 7).cases
    cap = P.default_max_rejects(10)
    assert cap == 5
    est = P.estimate_build(cases, PRICES, n=10, seed=7)
    assert est == P.estimate_build(cases, PRICES, n=10, seed=7, max_rejects=cap)
    assert estimate_tokens(longest_paraphrase(first[0].question)) == P.PARAPHRASE_MAX_TOKENS
    # Reject the first `cap` questions of the draw; every paraphrase is as long as allowed.
    models = Worst(reject={c.question for c in first[:cap]})
    res = build(cases, models, n=10)
    assert len(res.rejected) == cap and len(res.kept) == 10
    assert models.calls == 2 * (10 + cap)
    assert est >= _reservations(models) >= res.spent_usd > 0
    assert res.estimate_usd == pytest.approx(est, rel=1e-12)
    # Fewer rejects allowed: a smaller estimate; more rejects than the cap stop the build.
    assert P.estimate_build(cases, PRICES, n=10, seed=7, max_rejects=0) < est
    with pytest.raises(P.ProbeError, match="rejected"):
        build(cases, Worst(reject={c.question for c in first[: cap + 1]}), n=10)
    with pytest.raises(P.ProbeError, match="rejected"):
        build(cases, Worst(reject={first[0].question}), n=10, max_rejects=0)


def test_a_paraphrase_longer_than_the_estimate_allows_stops_the_build():
    cases = population()

    class TooLong(Models):
        def reply(self, req):
            if req.model_id == P.PARAPHRASE_MODEL:
                return "y" * (3 * P.PARAPHRASE_MAX_TOKENS)  # estimates at one token over
            return super().reply(req)

    models = TooLong()
    with pytest.raises(P.ProbeError, match="longer than"):
        build(cases, models, n=10)
    assert not models.anthropic.requests  # no check is sent for it


def test_estimate_is_an_upper_bound_from_the_price_table():
    cases = population()
    est = P.estimate_build(cases, PRICES, n=10, seed=7, max_rejects=0)
    first = stratified_sample(cases, 10, 7).cases
    node = P.ProbeCall({}, PRICES)
    ctx = P.context()
    by_hand = sum(
        node.estimate_cost(P.paraphrase_input(c, P.PARAPHRASE_MODEL), ctx)
        + node.estimate_cost(P.check_input(c, longest_paraphrase(c.question)), ctx)
        for c in first
    )
    # Sized with an empty paraphrase plus the longest one's tokens: never below the real prompt.
    assert est >= by_hand > 0
    assert est == pytest.approx(by_hand, rel=1e-3)


def test_builder_sends_gemmas_call_settings():
    """Item 4, aligned with gemma-grounded-health's build: paraphrases at effort low, checks
    at Opus 5.5's default effort (medium, D-24) with a 200-token output limit."""
    models = Models()
    build(population(), models, n=10)
    para, check = models.openai.requests, models.anthropic.requests
    assert para and check
    assert {(r.model_id, r.params["effort"], r.max_output_tokens) for r in para} == {
        (P.PARAPHRASE_MODEL, "low", P.PARAPHRASE_MAX_TOKENS)
    }
    assert {(r.model_id, r.params["effort"], r.max_output_tokens) for r in check} == {
        (P.CHECK_MODEL, "medium", 200)
    }
    assert (P.CHECK_EFFORT, P.CHECK_MAX_TOKENS, P.PARAPHRASE_EFFORT) == ("medium", 200, "low")


def test_paraphrase_is_the_first_line_verbatim_as_gemma_kept_it():
    cases = population()

    class Labelled(Models):
        def reply(self, req):
            if req.model_id == P.PARAPHRASE_MODEL:
                return f"\n  Rewritten: {_question(req.prompt)} (reworded)  \nsecond line"
            return super().reply(req)

    r = build(cases, Labelled(), n=10).kept[0]
    assert r.paraphrase == f"Rewritten: {r.original} (reworded)"


def test_identical_replies_are_rejected_not_kept():
    cases = population()
    first = stratified_sample(cases, 10, 7).cases

    class Same(Models):
        def reply(self, req):
            if req.model_id == P.PARAPHRASE_MODEL and _question(req.prompt) == first[1].question:
                return first[1].question.upper()  # same words: not a paraphrase
            return super().reply(req)

    res = build(cases, Same(), n=10)
    assert {r.pubid for r in res.rejected} == {first[1].id}


def test_an_unparseable_check_reply_stops_the_build_as_gemmas_did(tmp_path):
    """Item 4: gemma's parse_verdict raised on a reply that is neither SAME nor DIFFERENT, so
    the build stopped; it is not counted as a reject (that would change the draw)."""
    cases = population()
    first = stratified_sample(cases, 10, 7).cases

    class Odd(Models):
        def reply(self, req):
            if req.model_id == P.CHECK_MODEL and _original(req.prompt) == first[2].question:
                return "Probably the same"
            return super().reply(req)

    store = Store(tmp_path / "store")
    with pytest.raises(P.ProbeError, match=rf"{first[2].id}.*SAME or DIFFERENT"):
        build(cases, Odd(), store=store, n=10)
    again = Odd()
    with pytest.raises(P.ProbeError, match="SAME or DIFFERENT"):
        build(cases, again, store=store, n=10)
    assert again.calls == 0  # the round's calls were cached and counted, not lost


@pytest.mark.parametrize("kind", ["empty", "refusal", "truncated"])
def test_an_unusable_paraphrase_reply_stops_the_build(kind):
    """An empty or refused reply crashed gemma's build; a reply cut off at EvalKit's output cap
    (gemma set none) would have been longer there. All three stop the build."""
    cases = population()
    first = stratified_sample(cases, 10, 7).cases

    class Bad(Models):
        def reply(self, req):
            if req.model_id == P.PARAPHRASE_MODEL and _question(req.prompt) == first[4].question:
                if kind == "empty":
                    return "  \n "
                return RawCompletion(
                    model=req.model,
                    model_snapshot=req.model_id,
                    params=dict(req.params),
                    raw_text="" if kind == "refusal" else "Does treatment",
                    tokens_in=10,
                    tokens_out=5,
                    latency_ms=1,
                    cost_usd=0.001,
                    stop_reason="refusal" if kind == "refusal" else "length",
                    refusal=kind == "refusal",
                )
            return super().reply(req)

    models = Bad()
    with pytest.raises(P.ProbeError, match=first[4].id):
        build(cases, models, n=10)
    assert not any(_original(r.prompt) == first[4].question for r in models.anthropic.requests)


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


@pytest.mark.safety
def test_module_cli_build_exits_3_at_the_cost_cap_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """Item 7: `python -m evalkit.paraphrase build` stops at the cap with exit 3, after
    printing its estimate, and leaves no half-built set behind; the rerun resumes."""
    monkeypatch.setattr(P, "load_pqa_l", population)
    models = Models()
    monkeypatch.setattr(P, "_live_adapters", lambda providers, prices: models.adapters)
    out, store = tmp_path / "o", tmp_path / "s"
    argv = ["build", "--out", str(out), "--n", "10", "--store", str(store)]
    assert P.main([*argv, "--max-cost", "0.02"]) == 3
    err = capsys.readouterr().err
    assert err.index("estimate $") < err.index("stopped at the cost cap")
    assert "rerun with a higher --max-cost" in err
    assert not out.exists() and 0 < models.calls < 20
    assert P.main([*argv, "--max-cost", "10"]) == 0
    assert len(P.load_set(out).kept) == 10 and models.calls == 20  # nothing paid twice


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


def _completion(
    model: str,
    label: str | None,
    *,
    effort: str = "low",
    snapshot: str | None = None,
) -> Completion:
    """`label` None is a schema error; "!transport" or "!refusal" is that error."""
    error = None
    if label is None:
        error = "schema"
    elif label.startswith("!"):
        error = label[1:]
    return Completion(
        model=model,
        model_snapshot="" if error == "transport" else (snapshot or model),
        params={"effort": effort, "structured_output": True},
        raw_text="{}",
        answer=None if error else Answer(label=label or "", answer="a", citations=(1,)),
        error=error,  # type: ignore[arg-type]
        tokens_in=10,
        tokens_out=5,
        latency_ms=10,
        cost_usd=0.0,
    )


def write_run(
    folder: Path,
    run_id: str,
    cases: list[Case],
    answers: Mapping[str, Mapping[str, str | None]],
    *,
    task: str = "t",
    effort: Mapping[str, str] | None = None,
    snapshot: Mapping[str, str] | None = None,
    prompt_hash: str = "p",
) -> Path:
    """A run folder in EvalKit's format: run.json, cases.jsonl and records.jsonl (unscored).
    `effort` and `snapshot` set a model's recorded settings (default: effort low, snapshot =
    the model name)."""
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
                task=task,
                case_id=cid,
                timestamp=datetime(2026, 11, 17, tzinfo=UTC),
                completion=_completion(
                    model,
                    label,
                    effort=(effort or {}).get(model, "low"),
                    snapshot=(snapshot or {}).get(model),
                ),
                claims=(),
                verdicts=(),
                scores=None,
                seed=7,
                git_sha="",
                prompt_hash=prompt_hash,
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
    assert (a.n, b.n, rep.n_shared, rep.n_probe) == (40, 40, 40, 40)
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
    assert rep.models[0].n == 30 and rep.n_shared == 30
    assert rep.unpaired_models == ("model-b", "model-c")
    assert "30 of 40" in P.report_text(rep)


@pytest.mark.safety
@pytest.mark.parametrize("error", ["!transport", None, "!refusal"])
def test_errors_never_create_a_flag_and_are_excluded_from_the_pairs(tmp_path, error):
    """Item 1 (verifier repro): 100% right on the originals, 10 errored paraphrase completions
    and 30 right ones. Errors are counted, the 10 pairs are excluded, nothing is flagged."""
    from evalkit.stats import Resamples, interval, paired

    cases = pop40()
    probe = synthetic_set(tmp_path, cases, n=40)
    orig = P.probe_cases(cases, "original", probe)
    para = P.probe_cases(cases, "paraphrase", probe)
    gold = {c.id: c.reference_label or "" for c in orig}
    ids = [c.id for c in orig]
    a_para = {cid: (error if i < 10 else gold[cid]) for i, cid in enumerate(ids)}
    # model-b: 3 errors on the original side, one of them paired with a paraphrase error.
    b_orig = {cid: ("!transport" if i in (0, 20, 21) else gold[cid]) for i, cid in enumerate(ids)}
    b_para = {cid: ("!transport" if i == 0 else gold[cid]) for i, cid in enumerate(ids)}
    o = write_run(tmp_path / "o", "r-o", orig, {"model-a": dict(gold), "model-b": b_orig})
    p = write_run(tmp_path / "p", "r-p", para, {"model-a": a_para, "model-b": b_para})
    rep = P.contamination_report(o, p, probe=probe)
    a, b = (next(m for m in rep.models if m.model == name) for name in ("model-a", "model-b"))
    assert (a.n, a.n_excluded_errors, a.errors_original, a.errors_paraphrase) == (30, 10, 0, 10)
    assert (a.n_missing_original, a.n_missing_paraphrase) == (0, 0)
    assert a.accuracy_paraphrase.point == 1.0 and a.drop.point == 0.0
    assert not a.flagged and a.direction == "none" and a.signal == "none"
    assert a.exclusion_warning  # 10 of 40 pairs is over 5%
    # The comparison is exactly stats.paired on the 30 pairs where both sides answered.
    kept_ids = sorted(ids[10:])
    rs = Resamples(30, 10_000, seed=7)
    want, pv = paired([1.0] * 30, [1.0] * 30, rs)
    assert (a.drop, a.p_value, a.accuracy_original) == (want, pv, interval([1.0] * 30, rs))
    assert len(kept_ids) == a.n
    assert (b.n, b.n_excluded_errors, b.errors_original, b.errors_paraphrase) == (37, 3, 3, 1)
    assert b.exclusion_warning  # 3 of 40 = 7.5%
    text = P.report_text(rep)
    assert "WARNING" in text and "model-a" in text and "10 of 40 pairs" in text
    assert "errors: original 0, paraphrase 10" in text
    assert "FLAG" not in text


def test_exclusions_at_or_under_five_percent_do_not_warn(probe_runs, tmp_path):
    probe, o, _p, ids, gold = probe_runs
    para = P.probe_cases(pop40(), "paraphrase", probe)
    a_para = {cid: ("!transport" if i < 2 else gold[cid]) for i, cid in enumerate(ids)}
    p2 = write_run(tmp_path / "p2", "r-p2", para, {"model-a": a_para})
    a = P.contamination_report(o, p2, probe=probe).models[0]
    assert (a.n, a.n_excluded_errors, a.exclusion_warning) == (38, 2, False)  # 5.0%
    assert "WARNING" not in P.report_text(P.contamination_report(o, p2, probe=probe))


def test_coverage_is_reported_per_model_and_the_header_never_overclaims(probe_runs, tmp_path):
    """Item 2: per model, n paired, n missing on each side and n excluded for errors; the
    header says "all N paired" only when every model has every pair."""
    probe, o, p, ids, gold = probe_runs
    full = P.report_text(P.contamination_report(o, p, probe=probe))
    assert "all 40 probe questions paired for every model" in full
    para = P.probe_cases(pop40(), "paraphrase", probe)
    b_para = {cid: gold[cid] for i, cid in enumerate(ids) if i >= 15}  # 15 records missing
    p2 = write_run(tmp_path / "p2", "r-p2", para, {"model-a": dict(gold), "model-b": b_para})
    rep = P.contamination_report(o, p2, probe=probe)
    assert rep.n_shared == 40
    b = next(m for m in rep.models if m.model == "model-b")
    assert (b.n, b.n_missing_original, b.n_missing_paraphrase, b.n_excluded_errors) == (
        25,
        0,
        15,
        0,
    )
    text = P.report_text(rep)
    assert "all 40" not in text
    assert "40 of 40 probe questions in both runs" in text
    assert "model-b: 25 paired of 40; missing 0 original, 15 paraphrase" in text


def test_different_settings_make_a_flag_confounded(probe_runs, tmp_path):
    """Item 3: model-a's paraphrase run used another effort, prompt template and snapshot, so
    its drop is labelled confounded, not a contamination signal, unless allowed."""
    probe, o, _p, ids, gold = probe_runs
    para = P.probe_cases(pop40(), "paraphrase", probe)
    a_para = {cid: (wrong(gold[cid]) if i < 20 else gold[cid]) for i, cid in enumerate(ids)}
    b = {cid: (gold[cid] if i % 2 else wrong(gold[cid])) for i, cid in enumerate(ids)}
    p2 = write_run(
        tmp_path / "p2",
        "r-p2",
        para,
        {"model-a": a_para, "model-b": b},
        effort={"model-a": "high"},
        snapshot={"model-a": "model-a-2026-12-01"},
        prompt_hash="q",
    )
    rep = P.contamination_report(o, p2, probe=probe)
    a = next(m for m in rep.models if m.model == "model-a")
    assert a.flagged and a.direction == "drop" and a.signal == "confounded"
    diffs = " | ".join(a.settings_differences)
    assert "prompt template hash" in diffs and "effort" in diffs and "model snapshot" in diffs
    mb = next(m for m in rep.models if m.model == "model-b")
    assert mb.signal == "none" and "prompt template hash" in " | ".join(mb.settings_differences)
    text = P.report_text(rep)
    assert text.index("SETTINGS DIFFER") < text.index("drop in points")  # before the table
    assert "FLAG: confounded" in text and "FLAG: contamination signal" not in text
    allowed = P.contamination_report(o, p2, probe=probe, allow_different_settings=True)
    a2 = next(m for m in allowed.models if m.model == "model-a")
    assert a2.signal == "contamination" and a2.settings_differences == a.settings_differences
    assert allowed.allow_different_settings
    assert "SETTINGS DIFFER" in P.report_text(allowed)
    assert "FLAG: contamination signal" in P.report_text(allowed)


def test_same_settings_keep_a_flag_a_contamination_signal(probe_runs):
    probe, o, p, _ids, _gold = probe_runs
    rep = P.contamination_report(o, p, probe=probe)
    a = next(m for m in rep.models if m.model == "model-a")
    assert a.settings_differences == () and a.signal == "contamination"
    assert "SETTINGS DIFFER" not in P.report_text(rep)
    assert "FLAG: contamination signal" in P.report_text(rep)


@pytest.mark.contract
def test_cli_allow_different_settings(probe_runs, tmp_path, monkeypatch, capsys):
    probe, o, _p, ids, gold = probe_runs
    monkeypatch.setattr(P, "SET_DIR", Path(probe.folder))
    para = P.probe_cases(pop40(), "paraphrase", probe)
    a_para = {cid: (wrong(gold[cid]) if i < 20 else gold[cid]) for i, cid in enumerate(ids)}
    p2 = write_run(tmp_path / "p2", "r-p2", para, {"model-a": a_para}, effort={"model-a": "high"})
    assert cli.main(["report", "--paraphrase", str(o), str(p2), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["contamination"]["models"][0]["signal"] == (
        "confounded"
    )
    argv = ["report", "--paraphrase", str(o), str(p2), "--allow-different-settings", "--json"]
    assert cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)["contamination"]
    assert out["allow_different_settings"] and out["models"][0]["signal"] == "contamination"


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
    # Item 6: a refused --publish writes nothing (no contamination.json to mistake for final).
    assert not (p / "contamination.json").exists()
    assert cli.main(["report", "runs/x", "--paraphrase", str(o), str(p)]) == 2
