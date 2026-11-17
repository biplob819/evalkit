"""Paraphrase contamination probe (B5.8, PRD P4, DATA_CARD DAT-06, D-62, D-68).

PubMedQA is probably in every compared model's training data. The probe asks the same 100
PQA-L questions (same ids, contexts and labels) once in the original wording and once
paraphrased, and reports per model how much accuracy drops, with a paired bootstrap interval.

- PAR-01 the set: vendored from gemma-grounded-health c7d70d2 (D-68), built by DAT-06: 100
  questions from EvalKit's stratified sampler at seed 7 (55 yes, 34 no, 11 maybe); each
  rewritten once by `gpt-6.1-terra`; Claude Opus 5.5 checks, given the abstract, that the
  rewrite asks the same question; a rejected question is replaced by the next question of its
  label in the seeded order. Every load re-checks the set against PQA-L by replaying that
  method with the recorded verdicts. The builder (`build_probe`) rebuilds or extends it:
  cached in the store, prompts and models recorded by hash, the estimate printed before any
  call, the cost cap honoured.
- PAR-02 the report: `contamination_report(original_run, paraphrase_run)`, also
  `evalkit report --paraphrase ORIGINAL_RUN PARAPHRASE_RUN`. Pairs are questions where both
  runs produced an answer (errors are counted and excluded, never a flag); a flag from runs
  whose settings differ is "confounded". Provisional until the owner's spot-check of 20
  kept and every rejected paraphrase is recorded as passed in the provenance.

    python -m evalkit.paraphrase verify
    python -m evalkit.paraphrase build --out probe-set --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evalkit.adapters import CompletionRequest, ModelAdapter, RawCompletion
from evalkit.cost import Price, call_cost, estimate_tokens, load_prices, price_for
from evalkit.datasets.pubmedqa import DatasetError, load_pqa_l
from evalkit.runtime import Executor, Node, RunContext, Store
from evalkit.sampling import stratified_sample
from evalkit.schemas import Case, Interval, Record
from evalkit.schemas._base import FrozenMap, Model, Name, NonBlank, NonNegInt, PosInt
from evalkit.scorers.deterministic import label_correct
from evalkit.stats import Resamples, interval, paired

__all__ = [
    "CHECK_MODEL",
    "CHECK_PROMPT",
    "DATASETS",
    "PARAPHRASE_MODEL",
    "PARAPHRASE_PROMPT",
    "SEED",
    "SET_FILES",
    "BuildResult",
    "ContaminationReport",
    "ModelContamination",
    "N",
    "ProbeCall",
    "ProbeCallInput",
    "ProbeError",
    "ProbeRecord",
    "ProbeSet",
    "Provenance",
    "build_probe",
    "contamination_report",
    "estimate_build",
    "jsonl",
    "load_provenance",
    "load_set",
    "main",
    "probe_cases",
    "probe_problems",
    "prompt_hash",
    "report_text",
    "require_final",
    "write_probe",
]

N = 100
SEED = 7
N_RESAMPLES = 10_000
PARAPHRASE_MODEL = "gpt-6.1-terra"
CHECK_MODEL = "claude-opus-5-5"
METHOD = "DATA_CARD DAT-06"
SET_DIR = Path(__file__).parent / "tasks"  # the vendored set (read at call time: tests swap it)
VENDORED_DIR = SET_DIR
KEPT_FILE = "pubmedqa_paraphrase.jsonl"
REJECTS_FILE = "pubmedqa_paraphrase_rejects.jsonl"
PROVENANCE_FILE = "pubmedqa_paraphrase.provenance.json"
SET_FILES = (KEPT_FILE, REJECTS_FILE, PROVENANCE_FILE)
# Built-in task datasets (src/evalkit/tasks/pubmedqa-paraphrase[-original].yaml) -> wording.
DATASETS: dict[str, Literal["original", "paraphrase"]] = {
    "evalkit/pubmedqa-paraphrase": "paraphrase",
    "evalkit/pubmedqa-paraphrase-original": "original",
}
PARAPHRASE_SOURCE = "pubmedqa/pqa_l+paraphrase"

# The prompts gemma-grounded-health built the vendored set with (hashes 1bb383c050490b51 and
# 2331517ce31052a8 in every record). Changing a word changes the hash and fails verification.
PARAPHRASE_PROMPT = (
    "Rewrite the research question below so that it asks exactly the same thing. Keep its "
    "meaning, every entity, and every number; change the wording and the word order. Reply "
    "with the rewritten question only, on one line.\n\nQuestion: {question}"
)
CHECK_PROMPT = (
    "Here is the abstract of a study, an original research question about it, and a rewritten "
    "version of that question.\n\nAbstract:\n{context}\n\nOriginal: {original}\nRewritten: "
    "{paraphrase}\n\nGiven the abstract, does the rewritten question ask the same question as "
    "the original, so that the same answer (yes, no or maybe) is correct for both? Reply with "
    "one word, SAME or DIFFERENT, then a short reason on the next line."
)
# Call settings, aligned with gemma-grounded-health's build (src/ggh/evals/probe.py, c7d70d2):
# the paraphraser at reasoning effort low; the checker with max_tokens 200 and no effort sent,
# which on Opus 5.5 is its default, medium (D-24; EvalKit's adapters send effort explicitly).
# One difference stays: gemma set no output limit on the paraphraser, EvalKit caps it so the
# spend cap has an upper bound; a reply cut off at the cap stops the build (it would have been
# longer in gemma's build), so a capped reply never enters a set.
PARAPHRASE_EFFORT = "low"
PARAPHRASE_MAX_TOKENS = 1024  # reasoning tokens count as output on the paraphraser
CHECK_EFFORT = "medium"
CHECK_MAX_TOKENS = 200
# The longest paraphrase the builder sends to the checker, in `estimate_tokens` units (about
# 3,000 ASCII characters for a one-line question); the build estimate sizes every check with
# it, so a longer paraphrase stops the build instead of spending past the estimate.
PARAPHRASE_MAX_PROMPT_TOKENS = PARAPHRASE_MAX_TOKENS
TRUNCATED = frozenset({"length", "max_tokens"})  # provider stop reasons at the output cap
SPOT_CHECK_RULE = (
    "the owner checks 20 kept and every rejected paraphrase by hand (DATA_CARD DAT-06, D-68)"
)


class ProbeError(ValueError):
    """The probe set or a run folder does not meet the method; nothing is reported."""


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- the set ----------------------------------------------------------------------------------


class ProbeRecord(Model):
    """One checked paraphrase; field order is the vendored files' key order."""

    pubid: NonBlank  # the PQA-L case id, `pqa-<pubid>`
    reference_label: NonBlank
    original: NonBlank
    paraphrase: str
    paraphrase_model: NonBlank
    paraphrase_prompt_hash: NonBlank
    check_verdict: Literal["same", "different"]
    check_model: NonBlank
    check_prompt_hash: NonBlank
    check_reason: str


class ProvenanceFile(Model):
    rows: NonNegInt
    sha256: NonBlank
    source_path: str | None = None


class ProbeSample(Model):
    dataset: NonBlank
    label_mix: FrozenMap[int]
    n: PosInt
    sampler: NonBlank
    seed: int


class SpotCheck(Model):
    status: Literal["pending", "passed", "failed"]
    required: NonBlank
    by: str | None = None
    on: str | None = None


class Provenance(Model):
    built_spend_usd: float | None
    check_model: NonBlank
    check_prompt_hash: NonBlank
    files: FrozenMap[ProvenanceFile]
    licence: NonBlank
    method: NonBlank
    paraphrase_model: NonBlank
    paraphrase_prompt_hash: NonBlank
    sample: ProbeSample
    source_commit: str | None
    source_repo: NonBlank
    spot_check: SpotCheck


class ProbeSet(Model):
    kept: tuple[ProbeRecord, ...]
    rejected: tuple[ProbeRecord, ...]
    provenance: Provenance
    folder: str = ""

    @classmethod
    def of(cls, result: BuildResult) -> ProbeSet:
        """A freshly built set (not yet written), with the provenance `write_probe` records."""
        return cls(
            kept=tuple(result.kept),
            rejected=tuple(result.rejected),
            provenance=_provenance(result, {}),
        )


def jsonl(rows: Sequence[ProbeRecord]) -> str:
    """The vendored serialisation: one ASCII JSON object per line, in field order."""
    return "".join(json.dumps(r.model_dump(), ensure_ascii=True) + "\n" for r in rows)


def _read_records(path: Path) -> tuple[ProbeRecord, ...]:
    if not path.is_file():
        raise ProbeError(f"probe file not found: {path}")
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                out.append(ProbeRecord.model_validate_json(line))
            except ValueError as exc:
                raise ProbeError(f"{path}:{i}: not a probe record ({exc})") from None
    return tuple(out)


def load_provenance(folder: Path | None = None) -> Provenance:
    path = (folder or SET_DIR) / PROVENANCE_FILE
    if not path.is_file():
        raise ProbeError(f"provenance record not found: {path}")
    try:
        return Provenance.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProbeError(f"{path}: not a provenance record ({exc})") from None


def load_set(folder: Path | None = None) -> ProbeSet:
    """The kept and rejected records and their provenance; each file's sha256 and row count
    must match the provenance, so an edited file is refused before anything reads it."""
    folder = folder or SET_DIR
    prov = load_provenance(folder)
    for name in (KEPT_FILE, REJECTS_FILE):
        entry = prov.files.get(name)
        if entry is None:
            raise ProbeError(f"the provenance in {folder} does not record {name}")
        path = folder / name
        if not path.is_file():
            raise ProbeError(f"probe file not found: {path}")
        digest = _sha256(path)
        if digest != entry.sha256:
            raise ProbeError(
                f"{path}: sha256 {digest[:12]}… does not match the provenance "
                f"({entry.sha256[:12]}…); the probe set was edited"
            )
    kept, rejected = _read_records(folder / KEPT_FILE), _read_records(folder / REJECTS_FILE)
    for name, rows in ((KEPT_FILE, kept), (REJECTS_FILE, rejected)):
        if len(rows) != prov.files[name].rows:
            raise ProbeError(
                f"{folder / name}: {len(rows)} rows, provenance says {prov.files[name].rows}"
            )
    return ProbeSet(kept=kept, rejected=rejected, provenance=prov, folder=str(folder))


def _queues(cases: Sequence[Case], n: int, seed: int) -> tuple[list[Case], dict[str, list[Case]]]:
    """The first `n` by EvalKit's sampler, and per label the rest in a seeded order."""
    first = list(stratified_sample(cases, n, seed).cases)
    drawn = {c.id for c in first}
    spare: dict[str, list[Case]] = {}
    for c in stratified_sample(cases, len(cases), seed).cases:
        if c.id not in drawn:
            spare.setdefault(c.reference_label or "<unlabelled>", []).append(c)
    return first, spare


def _kept(row: ProbeRecord, case: Case) -> bool:
    """Only a SAME verdict on a rewrite that is not the original's own words is kept (gemma's
    rule: `para.lower() != question.strip().lower()`)."""
    return (
        row.check_verdict == "same"
        and bool(row.paraphrase.strip())
        and row.paraphrase.strip().lower() != case.question.strip().lower()
    )


def default_max_rejects(n: int) -> int:
    """The builder's reject cap: a tenth of the questions, at least 5 (gemma rejected 1 of
    101). Past it the paraphraser or checker is failing, so the build stops for a look."""
    return max(5, n // 10)


async def _select(
    first: Sequence[Case],
    spare: Mapping[str, Sequence[Case]],
    decide: Callable[[list[Case]], Awaitable[list[ProbeRecord]]],
    max_rejects: int | None = None,
) -> tuple[list[ProbeRecord], list[ProbeRecord]]:
    """DAT-06 in rounds: decide every pending question, replace each reject with the next
    question of its label. Same as one FIFO queue: replacements join in rejection order.
    With `max_rejects`, one reject more stops the build (ProbeError) before its replacement
    is drawn; the selection itself is unchanged, so a finished build is the method's."""
    queues = {k: list(v) for k, v in spare.items()}
    kept: list[ProbeRecord] = []
    rejected: list[ProbeRecord] = []
    todo = list(first)
    while todo:
        rows = await decide(todo)
        nxt: list[Case] = []
        for case, row in zip(todo, rows, strict=True):
            if _kept(row, case):
                kept.append(row)
                continue
            rejected.append(row)
            if max_rejects is not None and len(rejected) > max_rejects:
                raise ProbeError(
                    f"{len(rejected)} questions rejected, over the cap of {max_rejects} "
                    f"(latest {row.pubid}: {row.check_reason or row.check_verdict}); the "
                    "paraphraser or checker is failing: inspect the replies (cached) before "
                    "rerunning with a higher --max-rejects"
                )
            label = case.reference_label or "<unlabelled>"
            if not queues.get(label):
                raise ProbeError(f"no replacement question left for label {label!r}")
            nxt.append(queues[label].pop(0))
        todo = nxt
    order = {c.id: i for i, c in enumerate(first)}
    kept.sort(key=lambda r: order.get(r.pubid, len(order)))  # stable: replacements keep order
    return kept, rejected


class _NoRecord(Exception):
    pass


def probe_problems(
    cases: Sequence[Case], probe: ProbeSet, *, n: int | None = None, seed: int | None = None
) -> list[str]:
    """Everything that makes `probe` differ from DAT-06 on these PQA-L cases (empty: valid)."""
    prov = probe.provenance
    n = prov.sample.n if n is None else n
    seed = prov.sample.seed if seed is None else seed
    problems: list[str] = []
    by_id = {c.id: c for c in cases}
    rows = (*probe.kept, *probe.rejected)
    seen: set[str] = set()
    for r in rows:
        if r.pubid in seen:
            problems.append(f"{r.pubid}: more than one record")
        seen.add(r.pubid)
        case = by_id.get(r.pubid)
        if case is None:
            problems.append(f"{r.pubid}: not in PQA-L")
            continue
        if r.original != case.question:
            problems.append(f"{r.pubid}: original question differs from PQA-L's")
        if r.reference_label != case.reference_label:
            problems.append(
                f"{r.pubid}: label {r.reference_label!r} but PQA-L says {case.reference_label!r}"
            )
        if r.paraphrase_prompt_hash != prompt_hash(PARAPHRASE_PROMPT):
            problems.append(f"{r.pubid}: paraphrase prompt hash is not the builder prompt's")
        if r.check_prompt_hash != prompt_hash(CHECK_PROMPT):
            problems.append(f"{r.pubid}: check prompt hash is not the builder prompt's")
        if (r.paraphrase_model, r.check_model) != (prov.paraphrase_model, prov.check_model):
            problems.append(f"{r.pubid}: models differ from the provenance")
    for r in probe.kept:
        if r.pubid in by_id and not _kept(r, by_id[r.pubid]):
            problems.append(f"{r.pubid}: kept without a SAME verdict on a real rewrite")
    if (prov.paraphrase_prompt_hash, prov.check_prompt_hash) != (
        prompt_hash(PARAPHRASE_PROMPT),
        prompt_hash(CHECK_PROMPT),
    ):
        problems.append("provenance prompt hashes are not the builder prompts'")
    if len(probe.kept) != n:
        problems.append(f"{len(probe.kept)} kept, the method keeps {n}")
    if problems:
        return problems
    if not 0 < n <= len(cases):
        return [f"cannot draw {n} questions from {len(cases)} cases"]
    mix = stratified_sample(cases, n, seed).label_mix
    got = {label: sum(r.reference_label == label for r in probe.kept) for label in mix}
    if got != dict(mix) or dict(prov.sample.label_mix) != dict(mix):
        problems.append(
            f"label mix {got} (provenance {dict(prov.sample.label_mix)}) is not "
            f"the sampler's {dict(mix)}"
        )
    records = {r.pubid: r for r in rows}

    async def replay(todo: list[Case]) -> list[ProbeRecord]:
        out = []
        for c in todo:
            if c.id not in records:
                raise _NoRecord(c.id)
            out.append(records[c.id])
        return out

    first, spare = _queues(cases, n, seed)
    try:
        kept, rejected = asyncio.run(_select(first, spare, replay))
    except _NoRecord as exc:
        return [*problems, f"set does not follow the method: {exc} is drawn but has no record"]
    except ProbeError as exc:
        return [*problems, f"set does not follow the method: {exc}"]
    if [r.pubid for r in kept] != [r.pubid for r in probe.kept]:
        problems.append(
            "set does not follow the method: the kept questions or their order "
            "differ from the seeded draw with replacements"
        )
    if [r.pubid for r in rejected] != [r.pubid for r in probe.rejected]:
        problems.append(
            "set does not follow the method: the rejected questions differ from the replay"
        )
    return problems


def probe_cases(
    cases: Sequence[Case],
    wording: Literal["original", "paraphrase"],
    probe: ProbeSet | None = None,
) -> list[Case]:
    """The probe's cases in one wording: PQA-L's ids, contexts and labels; only the question
    differs. The set is verified first; any problem raises ProbeError."""
    if wording not in ("original", "paraphrase"):
        raise ValueError(f"wording must be 'original' or 'paraphrase', got {wording!r}")
    probe = probe or load_set()
    problems = probe_problems(cases, probe)
    if problems:
        shown = "; ".join(problems[:5]) + (
            f"; … {len(problems) - 5} more" if len(problems) > 5 else ""
        )
        raise ProbeError(f"the paraphrase probe set does not match PQA-L: {shown}")
    by_id = {c.id: c for c in cases}
    if wording == "original":
        return [by_id[r.pubid] for r in probe.kept]
    return [
        by_id[r.pubid].model_copy(update={"question": r.paraphrase, "source": PARAPHRASE_SOURCE})
        for r in probe.kept
    ]


# --- the builder --------------------------------------------------------------------------------


class ProbeCallInput(Model):
    step: Literal["paraphrase", "check"]
    model_id: Name
    provider: Name
    prompt: str
    effort: Name
    max_output_tokens: PosInt


class ProbeCall(Node[ProbeCallInput, RawCompletion]):
    """One paraphrase or check call. Transport errors propagate (never cached), so a rerun
    tries again; finished calls replay from the store at no cost."""

    name = "paraphrase_probe_call"
    code_version = "1"
    In = ProbeCallInput
    Out = RawCompletion

    def __init__(self, adapters: Mapping[str, ModelAdapter], prices: dict[str, Price]) -> None:
        self.adapters = adapters
        self.prices = prices

    def pool(self, inp: ProbeCallInput) -> str | None:
        return inp.provider

    def estimate_cost(self, inp: ProbeCallInput, ctx: RunContext) -> float:
        # Upper bound: ~1 token per 3 prompt characters, plus the whole output budget.
        return call_cost(
            price_for(self.prices, inp.model_id),
            tokens_in=estimate_tokens(inp.prompt),
            tokens_out=inp.max_output_tokens,
        )

    async def run(self, inp: ProbeCallInput, ctx: RunContext) -> RawCompletion:
        req = CompletionRequest(
            model=inp.model_id,
            model_id=inp.model_id,
            prompt=inp.prompt,
            max_output_tokens=inp.max_output_tokens,
            params={"effort": inp.effort},
        )
        return await self.adapters[inp.provider].complete(req)


def _provider(model_id: str) -> str:
    from evalkit.pipeline.generate import provider_for

    return provider_for(model_id)


def paraphrase_input(case: Case, model_id: str = PARAPHRASE_MODEL) -> ProbeCallInput:
    return ProbeCallInput(
        step="paraphrase",
        model_id=model_id,
        provider=_provider(model_id),
        prompt=PARAPHRASE_PROMPT.format(question=case.question),
        effort=PARAPHRASE_EFFORT,
        max_output_tokens=PARAPHRASE_MAX_TOKENS,
    )


def check_input(case: Case, paraphrase: str, model_id: str = CHECK_MODEL) -> ProbeCallInput:
    ctx = "\n".join(f"[{i}] {s}" for i, s in enumerate(case.context, 1))
    prompt = CHECK_PROMPT.format(context=ctx, original=case.question, paraphrase=paraphrase)
    return ProbeCallInput(
        step="check",
        model_id=model_id,
        provider=_provider(model_id),
        prompt=prompt,
        effort=CHECK_EFFORT,
        max_output_tokens=CHECK_MAX_TOKENS,
    )


def context(step: str = "probe") -> RunContext:
    return RunContext(run_id=f"paraphrase-probe-{step}")


def _paraphrase_text(raw: RawCompletion, case: Case) -> str:
    """The reply's first line, stripped, kept verbatim as gemma's build kept it
    (`content.strip().splitlines()[0]`). A refused, empty or cut-off reply, or one longer than
    the estimate allows, stops the build: gemma's build crashed on an empty reply and set no
    output cap, so the method has no rule for these and the owner decides."""
    if raw.refusal:
        raise ProbeError(f"{case.id}: the paraphraser refused (reply cached; the build stops)")
    if raw.stop_reason in TRUNCATED:
        raise ProbeError(
            f"{case.id}: the paraphrase reply hit the {PARAPHRASE_MAX_TOKENS}-token output cap "
            "(gemma's build set none, so its reply would have been longer); the build stops"
        )
    lines = raw.raw_text.strip().splitlines()
    text = lines[0].strip() if lines else ""
    if not text:
        raise ProbeError(f"{case.id}: the paraphraser returned no text; the build stops")
    if estimate_tokens(text) > PARAPHRASE_MAX_PROMPT_TOKENS:
        raise ProbeError(
            f"{case.id}: the paraphrase is longer than the {PARAPHRASE_MAX_PROMPT_TOKENS} "
            "estimated tokens the build estimate allows for; the build stops"
        )
    return text


def _verdict(raw: RawCompletion, case: Case) -> tuple[bool, str]:
    """gemma's parse_verdict: the first line must be SAME or DIFFERENT, else the build stops
    (the reply is cached; a rerun with a fresh --store asks again)."""
    first, _, rest = raw.raw_text.strip().partition("\n")
    word = first.strip().strip("*. ").upper()
    if word not in ("SAME", "DIFFERENT"):
        raise ProbeError(
            f"{case.id}: check reply does not start with SAME or DIFFERENT: {first[:60]!r}; "
            "the build stops (gemma's method has no rule for it)"
        )
    return word == "SAME", rest.strip()


def _question_bound(
    node: ProbeCall, case: Case, paraphrase_model: str, check_model: str, ctx: RunContext
) -> float:
    """Upper bound on what the executor reserves for one question: its paraphrase call, and
    a check whose prompt holds the longest paraphrase the builder accepts. `estimate_tokens`
    is subadditive (each part's floor loses under one token and every count adds one), so
    the check prompt with the paraphrase in place never estimates above the prompt with an
    empty paraphrase plus PARAPHRASE_MAX_PROMPT_TOKENS."""
    empty = check_input(case, "", check_model)
    check = call_cost(
        price_for(node.prices, check_model),
        tokens_in=estimate_tokens(empty.prompt) + PARAPHRASE_MAX_PROMPT_TOKENS,
        tokens_out=empty.max_output_tokens,
    )
    return node.estimate_cost(paraphrase_input(case, paraphrase_model), ctx) + check


def estimate_build(
    cases: Sequence[Case],
    prices: dict[str, Price],
    *,
    n: int = N,
    seed: int = SEED,
    max_rejects: int | None = None,
    paraphrase_model: str = PARAPHRASE_MODEL,
    check_model: str = CHECK_MODEL,
) -> float:
    """Strict upper-bound USD on what a build can reserve (and so spend): the first `n`
    questions, each with its paraphrase call and a worst-case check (the longest paraphrase
    the builder accepts), plus a replacement for every reject up to the reject cap
    (`default_max_rejects(n)` unless given): the costliest `max_rejects` of the questions
    that could be drawn (the first `max_rejects` spares of each label)."""
    k = default_max_rejects(n) if max_rejects is None else max_rejects
    node = ProbeCall({}, prices)
    ctx = context()
    first, spare = _queues(cases, n, seed)
    total = sum(_question_bound(node, c, paraphrase_model, check_model, ctx) for c in first)
    candidates = sorted(
        (
            _question_bound(node, c, paraphrase_model, check_model, ctx)
            for queue in spare.values()
            for c in queue[:k]
        ),
        reverse=True,
    )
    return total + sum(candidates[:k])


@dataclass(frozen=True)
class BuildResult:
    kept: tuple[ProbeRecord, ...]
    rejected: tuple[ProbeRecord, ...]
    n: int
    seed: int
    paraphrase_model: str
    check_model: str
    estimate_usd: float
    spent_usd: float
    calls: int
    cache_hits: int
    max_rejects: int = 0


async def build_probe(
    cases: Sequence[Case],
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    store: Store,
    *,
    n: int = N,
    seed: int = SEED,
    max_cost_usd: float | None = 2.0,
    max_rejects: int | None = None,
    paraphrase_model: str = PARAPHRASE_MODEL,
    check_model: str = CHECK_MODEL,
    concurrency: int = 8,
    echo: Callable[[str], object] = print,
) -> BuildResult:
    """Build (or extend, with a larger `n`) the set by DAT-06. The estimate and cap are passed
    to `echo` before the first call; the executor stops at the cap with BudgetExceeded and
    every finished call stays cached, so the same command resumes. More than `max_rejects`
    rejections (default `default_max_rejects(n)`), or a reply the method cannot use, stops
    the build with ProbeError (the round's calls stay cached and counted)."""
    k = default_max_rejects(n) if max_rejects is None else max_rejects
    first, spare = _queues(cases, n, seed)
    est = estimate_build(
        cases,
        prices,
        n=n,
        seed=seed,
        max_rejects=k,
        paraphrase_model=paraphrase_model,
        check_model=check_model,
    )
    cap = "no cap" if max_cost_usd is None else f"cap ${max_cost_usd:.2f}"
    echo(
        f"paraphrase probe: {n} questions (seed {seed}), {paraphrase_model} paraphrases, "
        f"{check_model} checks; estimate ${est:.2f} upper bound (worst-case checks and up to "
        f"{k} replacement draws; more rejects stop the build); {cap}; cached calls are free"
    )
    if max_cost_usd is not None and est > max_cost_usd:
        echo(
            f"note: estimate ${est:.2f} exceeds the cap ${max_cost_usd:.2f}; the build stops "
            "at the cap and resumes with a higher --max-cost"
        )
    providers = {_provider(paraphrase_model), _provider(check_model)}
    ex = Executor(
        store, concurrency=dict.fromkeys(providers, concurrency), max_cost_usd=max_cost_usd
    )
    node = ProbeCall(adapters, prices)
    p_hash, c_hash = prompt_hash(PARAPHRASE_PROMPT), prompt_hash(CHECK_PROMPT)

    async def decide(todo: list[Case]) -> list[ProbeRecord]:
        raws = await ex.map(
            node, [paraphrase_input(c, paraphrase_model) for c in todo], context("paraphrase")
        )
        paras = [_paraphrase_text(r, c) for r, c in zip(raws, todo, strict=True)]
        checks = await ex.map(
            node,
            [check_input(c, p, check_model) for c, p in zip(todo, paras, strict=True)],
            context("check"),
        )
        rows = []
        for c, para, raw in zip(todo, paras, checks, strict=True):
            same, reason = _verdict(raw, c)
            rows.append(
                ProbeRecord(
                    pubid=c.id,
                    reference_label=c.reference_label or "",
                    original=c.question,
                    paraphrase=para,
                    paraphrase_model=paraphrase_model,
                    paraphrase_prompt_hash=p_hash,
                    check_verdict="same" if same else "different",
                    check_model=check_model,
                    check_prompt_hash=c_hash,
                    check_reason=reason,
                )
            )
        return rows

    kept, rejected = await _select(first, spare, decide, max_rejects=k)
    return BuildResult(
        kept=tuple(kept),
        rejected=tuple(rejected),
        n=n,
        seed=seed,
        paraphrase_model=paraphrase_model,
        check_model=check_model,
        estimate_usd=est,
        spent_usd=ex.spent_usd,
        calls=ex.calls,
        cache_hits=ex.cache_hits,
        max_rejects=k,
    )


def _provenance(result: BuildResult, files: dict[str, ProvenanceFile]) -> Provenance:
    from evalkit import __version__

    mix: dict[str, int] = {}
    for r in result.kept:
        mix[r.reference_label] = mix.get(r.reference_label, 0) + 1
    return Provenance(
        built_spend_usd=round(result.spent_usd, 6),
        check_model=result.check_model,
        check_prompt_hash=prompt_hash(CHECK_PROMPT),
        files=files,
        licence="PubMedQA (Jin et al., 2019), MIT: questions, contexts and labels from PQA-L "
        "(qiaojin/PubMedQA, pqa_labeled)",
        method=METHOD,
        paraphrase_model=result.paraphrase_model,
        paraphrase_prompt_hash=prompt_hash(PARAPHRASE_PROMPT),
        sample=ProbeSample(
            dataset="pubmedqa/pqa_l",
            label_mix=dict(sorted(mix.items())),
            n=result.n,
            sampler="evalkit.sampling.stratified_sample",
            seed=result.seed,
        ),
        source_commit=None,
        source_repo=f"evalkit {__version__} (evalkit.paraphrase.build_probe)",
        spot_check=SpotCheck(status="pending", required=SPOT_CHECK_RULE),
    )


def write_probe(out_dir: Path, result: BuildResult) -> Provenance:
    """Write the kept and rejected files (vendored serialisation) and a provenance record with
    the spot-check pending. Never writes into the package's vendored set."""
    if out_dir.resolve() == VENDORED_DIR.resolve():
        raise ProbeError("refusing to overwrite the vendored probe set; choose another --out")
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, rows in ((KEPT_FILE, result.kept), (REJECTS_FILE, result.rejected)):
        (out_dir / name).write_text(jsonl(rows), encoding="utf-8")
        files[name] = ProvenanceFile(rows=len(rows), sha256=_sha256(out_dir / name))
    prov = _provenance(result, files)
    (out_dir / PROVENANCE_FILE).write_text(
        json.dumps(prov.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return prov


# --- the report -----------------------------------------------------------------------------------

EXCLUSION_WARNING = 0.05  # warn when errors exclude more than 5% of a model's pairs
# What each run did for a model, compared between the two runs. Task names are not compared:
# they differ by design (pubmedqa or pubmedqa-paraphrase-original vs pubmedqa-paraphrase); the
# prompt template a task contributes is compared by its hash.
SETTINGS = ("prompt template hash", "completion params", "model snapshot")


class ModelContamination(Model):
    model: NonBlank
    n: PosInt  # pairs where both wordings produced an answer: the comparison's n
    n_missing_original: NonNegInt  # probe questions in both runs without this model's record
    n_missing_paraphrase: NonNegInt
    n_excluded_errors: NonNegInt  # pairs left out because either side's completion errored
    errors_original: NonNegInt  # errored completions on the shared probe questions
    errors_paraphrase: NonNegInt
    exclusion_warning: bool  # errors excluded more than 5% of the pairs with both records
    accuracy_original: Interval
    accuracy_paraphrase: Interval
    drop: Interval  # original - paraphrase; positive means accuracy fell on the new wording
    p_value: float
    flagged: bool  # the 95% interval on the drop excludes zero
    direction: Literal["drop", "rise", "none"]
    settings_differences: tuple[str, ...]  # what the two runs did differently for this model
    # How to read a flag: "confounded" when the settings differ (unless allowed), else the
    # direction ("contamination" for a drop, "rise"), "none" when not flagged.
    signal: Literal["contamination", "rise", "confounded", "none"]


class ContaminationReport(Model):
    original_run: NonBlank
    paraphrase_run: NonBlank
    original_run_id: NonBlank
    paraphrase_run_id: NonBlank
    original_tasks: tuple[str, ...]
    paraphrase_tasks: tuple[str, ...]
    n_probe: NonNegInt
    n_shared: NonNegInt  # probe questions present in both runs' cases (per-model n is below)
    n_resamples: PosInt
    seed: int
    set_sha256: NonBlank
    source_repo: NonBlank
    source_commit: str | None
    spot_check: Literal["pending", "passed", "failed"]
    status: Literal["provisional", "final"]
    allow_different_settings: bool
    models: tuple[ModelContamination, ...]
    unpaired_models: tuple[str, ...]  # in one run only, or no pair where both sides answered


def _read_run(run_dir: Path) -> tuple[str, dict[str, Case], list[Record]]:
    for name in ("run.json", "cases.jsonl", "records.jsonl"):
        if not (run_dir / name).is_file():
            raise ProbeError(f"{run_dir} is not a run folder: {name} is missing")
    run_id = str(json.loads((run_dir / "run.json").read_text("utf-8"))["run_id"])
    cases = {}
    for line in (run_dir / "cases.jsonl").read_text("utf-8").splitlines():
        if line.strip():
            c = Case.model_validate_json(line)
            cases[c.id] = c
    records = [
        Record.model_validate_json(line)
        for line in (run_dir / "records.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    return run_id, cases, records


def _by_model(
    run_dir: Path, records: Sequence[Record], ids: set[str]
) -> dict[str, dict[str, Record]]:
    """model -> case id -> record, for the given case ids."""
    out: dict[str, dict[str, Record]] = {}
    for r in records:
        if r.case_id not in ids:
            continue
        per = out.setdefault(r.completion.model, {})
        if r.case_id in per:
            raise ProbeError(f"{run_dir}: duplicate record {r.case_id}/{r.completion.model}")
        per[r.case_id] = r
    return out


def _settings(records: Iterable[Record]) -> dict[str, tuple[str, ...]]:
    """The distinct values of each setting across a model's records in one run. A transport
    failure records no snapshot and adds its exception name to params; both are ignored."""
    seen: dict[str, set[str]] = {k: set() for k in SETTINGS}
    for r in records:
        c = r.completion
        seen["prompt template hash"].add(r.prompt_hash)
        params = {k: v for k, v in c.params.items() if k != "transport_error"}
        seen["completion params"].add(json.dumps(params, sort_keys=True))
        if c.model_snapshot:
            seen["model snapshot"].add(c.model_snapshot)
    return {k: tuple(sorted(v)) for k, v in seen.items()}


def _differences(
    original: Mapping[str, tuple[str, ...]], paraphrase: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    out = []
    for key in SETTINGS:
        a, b = original[key], paraphrase[key]
        if a and b and a != b:  # a side with no value (every call failed) cannot be compared
            out.append(f"{key}: original {' | '.join(a)} vs paraphrase {' | '.join(b)}")
    return tuple(out)


def _errored(r: Record) -> bool:
    return r.completion.error is not None


def contamination_report(
    original_run: Path,
    paraphrase_run: Path,
    *,
    probe: ProbeSet | None = None,
    n_resamples: int = N_RESAMPLES,
    seed: int = SEED,
    allow_different_settings: bool = False,
) -> ContaminationReport:
    """Original-vs-paraphrase accuracy per model, with a paired bootstrap 95% interval on the
    drop (DATA_CARD DAT-06), on the probe pubids where both runs produced an answer. Errored
    completions (transport, schema, refusal) are counted per wording and their pairs are
    excluded, never scored as wrong, so an error cannot create a flag. Each model's settings
    in the two runs are compared; a flag with different settings is "confounded", not a
    contamination signal, unless `allow_different_settings`."""
    probe = probe or load_set()
    spot = probe.provenance.spot_check.status
    if spot == "failed":
        raise ProbeError(
            "the owner spot-check of this probe set failed; rebuild the set before reporting"
        )
    by_pubid = {r.pubid: r for r in probe.kept}
    o_id, o_cases, o_recs = _read_run(original_run)
    p_id, p_cases, p_recs = _read_run(paraphrase_run)
    extra = sorted(set(p_cases) - set(by_pubid))
    if extra:
        raise ProbeError(
            f"{paraphrase_run} holds cases that are not in the probe set "
            f"({', '.join(extra[:3])}); run the pubmedqa-paraphrase task"
        )
    shared = sorted(set(o_cases) & set(p_cases) & set(by_pubid))
    if not shared:
        raise ProbeError(f"no probe pubid is in both {original_run} and {paraphrase_run}")
    for cid in shared:
        r, o, p = by_pubid[cid], o_cases[cid], p_cases[cid]
        if o.question != r.original:
            raise ProbeError(f"{original_run}: {cid} does not use the original wording")
        if p.question != r.paraphrase:
            raise ProbeError(f"{paraphrase_run}: {cid} does not use the paraphrased wording")
        if (o.context, o.reference_label) != (p.context, p.reference_label):
            raise ProbeError(f"{cid}: contexts or labels differ between the two runs")
    ids = set(shared)
    o_by = _by_model(original_run, o_recs, ids)
    p_by = _by_model(paraphrase_run, p_recs, ids)
    resamples: dict[int, Resamples] = {}
    models = []
    unpaired = []
    for m in sorted(set(o_by) | set(p_by)):
        o_m, p_m = o_by.get(m, {}), p_by.get(m, {})
        both = sorted(set(o_m) & set(p_m))
        pair_ids = [c for c in both if not _errored(o_m[c]) and not _errored(p_m[c])]
        if not pair_ids:
            unpaired.append(m)
            continue
        n = len(pair_ids)
        excluded = len(both) - n
        rs = resamples.get(n) or resamples.setdefault(n, Resamples(n, n_resamples, seed=seed))
        a = [label_correct(o_cases[c], o_m[c].completion).value for c in pair_ids]
        b = [label_correct(p_cases[c], p_m[c].completion).value for c in pair_ids]
        drop, pv = paired(a, b, rs)
        direction: Literal["drop", "rise", "none"] = (
            "drop" if drop.lo > 0 else "rise" if drop.hi < 0 else "none"
        )
        diffs = _differences(_settings(o_m.values()), _settings(p_m.values()))
        signal: Literal["contamination", "rise", "confounded", "none"]
        if direction == "none":
            signal = "none"
        elif diffs and not allow_different_settings:
            signal = "confounded"
        else:
            signal = "contamination" if direction == "drop" else "rise"
        models.append(
            ModelContamination(
                model=m,
                n=n,
                n_missing_original=len(ids - set(o_m)),
                n_missing_paraphrase=len(ids - set(p_m)),
                n_excluded_errors=excluded,
                errors_original=sum(_errored(r) for r in o_m.values()),
                errors_paraphrase=sum(_errored(r) for r in p_m.values()),
                exclusion_warning=excluded > EXCLUSION_WARNING * len(both),
                accuracy_original=interval(a, rs),
                accuracy_paraphrase=interval(b, rs),
                drop=drop,
                p_value=pv,
                flagged=direction != "none",
                direction=direction,
                settings_differences=diffs,
                signal=signal,
            )
        )
    kept_sha = probe.provenance.files.get(KEPT_FILE)
    return ContaminationReport(
        original_run=str(original_run),
        paraphrase_run=str(paraphrase_run),
        original_run_id=o_id,
        paraphrase_run_id=p_id,
        original_tasks=tuple(sorted({r.task for r in o_recs if r.case_id in ids})),
        paraphrase_tasks=tuple(sorted({r.task for r in p_recs if r.case_id in ids})),
        n_probe=len(probe.kept),
        n_shared=len(shared),
        n_resamples=n_resamples,
        seed=seed,
        set_sha256=kept_sha.sha256
        if kept_sha
        else hashlib.sha256(jsonl(probe.kept).encode()).hexdigest(),
        source_repo=probe.provenance.source_repo,
        source_commit=probe.provenance.source_commit,
        spot_check=spot,
        status="final" if spot == "passed" else "provisional",
        allow_different_settings=allow_different_settings,
        models=tuple(models),
        unpaired_models=tuple(unpaired),
    )


def require_final(report: ContaminationReport) -> None:
    if report.status != "final":
        raise ProbeError(
            f"the paraphrase probe is provisional: the owner spot-check is {report.spot_check} "
            f"({SPOT_CHECK_RULE}); results cannot be published until it has passed"
        )


def _pts(x: float) -> str:
    return f"{x * 100:+.1f}"


FLAGS = {
    "contamination": "  FLAG: contamination signal",
    "rise": "  FLAG: accuracy rose on the paraphrase (check the set)",
    "confounded": "  FLAG: confounded (the runs' settings differ; not a contamination signal)",
    "none": "",
}


def report_text(report: ContaminationReport) -> str:
    if report.status == "final":
        head = "FINAL: owner spot-check passed (DATA_CARD DAT-06)."
    else:
        head = (
            f"PROVISIONAL: owner spot-check {report.spot_check} ({SPOT_CHECK_RULE}); "
            "not final, do not publish."
        )
    complete = (
        report.n_shared == report.n_probe
        and not report.unpaired_models
        and all(m.n == report.n_probe for m in report.models)
    )
    cover = (
        f"all {report.n_probe} probe questions paired for every model"
        if complete
        else f"{report.n_shared} of {report.n_probe} probe questions in both runs; "
        "pairs per model below"
    )
    tasks = f"{', '.join(report.original_tasks)} vs {', '.join(report.paraphrase_tasks)}"
    lines = [
        head,
        f"paraphrase probe: {cover} (original {report.original_run_id}, paraphrase "
        f"{report.paraphrase_run_id}; tasks {tasks}); paired bootstrap, "
        f"{report.n_resamples:,} resamples, seed {report.seed}",
        "n counts the questions both runs answered without an error; errored completions are "
        "counted and their pairs excluded, never scored as wrong",
    ]
    for m in report.models:
        if m.settings_differences:
            tail = (
                "--allow-different-settings given: flags are read as signals anyway"
                if report.allow_different_settings
                else "A flag is labelled confounded, not a contamination signal "
                "(--allow-different-settings overrides)"
            )
            lines.append(
                f"SETTINGS DIFFER for {m.model}: {'; '.join(m.settings_differences)}. {tail}"
            )
    lines.append(
        f"{'model':<24} {'n':>4}  {'original':>8}  {'paraphrase':>10}  drop in points [95% CI]"
    )
    for m in report.models:
        lines.append(
            f"{m.model:<24} {m.n:>4}  {m.accuracy_original.point:>8.1%}  "
            f"{m.accuracy_paraphrase.point:>10.1%}  {_pts(m.drop.point)} "
            f"[{_pts(m.drop.lo)}, {_pts(m.drop.hi)}]{FLAGS[m.signal]}"
        )
    lines.append("coverage:")
    for m in report.models:
        lines.append(
            f"  {m.model}: {m.n} paired of {report.n_shared}; missing {m.n_missing_original} "
            f"original, {m.n_missing_paraphrase} paraphrase; {m.n_excluded_errors} excluded "
            f"for errors (errors: original {m.errors_original}, paraphrase "
            f"{m.errors_paraphrase})"
        )
    for m in report.models:
        if m.exclusion_warning:
            pairs = m.n + m.n_excluded_errors
            lines.append(
                f"WARNING: {m.model}: {m.n_excluded_errors} of {pairs} pairs "
                f"({m.n_excluded_errors / pairs:.1%}) excluded for errors, over "
                f"{EXCLUSION_WARNING:.0%}; the comparison covers only the {m.n} where both "
                "wordings produced an answer"
            )
    if report.unpaired_models:
        lines.append(
            "not in both runs, or no pair where both wordings answered: "
            + ", ".join(report.unpaired_models)
        )
    return "\n".join(lines)


# --- python -m evalkit.paraphrase ----------------------------------------------------------------


def _live_adapters(providers: set[str], prices: dict[str, Price]) -> dict[str, ModelAdapter]:
    """Real provider adapters for a paid build (never used in tests)."""
    from evalkit.adapters import get_adapter
    from evalkit.env import load_dotenv, missing_keys

    load_dotenv(Path.cwd() / ".env")
    if missing := missing_keys(providers):
        raise ProbeError(f"missing API keys: {', '.join(missing)}")
    return {p: get_adapter(p, prices=prices) for p in providers}  # pragma: no cover


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evalkit.paraphrase",
        description="Paraphrase contamination probe (DATA_CARD DAT-06).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="check the vendored set against PQA-L")
    b = sub.add_parser("build", help="rebuild or extend the set by the DAT-06 method")
    b.add_argument("--out", required=True, help="folder for the new set (not the vendored one)")
    b.add_argument("--n", type=int, default=N, help=f"questions to keep (default {N})")
    b.add_argument("--seed", type=int, default=SEED)
    b.add_argument("--max-cost", type=float, default=2.0, help="USD cap (default 2)")
    b.add_argument(
        "--max-rejects",
        type=int,
        default=None,
        help="stop after this many rejections (default: a tenth of --n, at least 5)",
    )
    b.add_argument("--store", default=".evalkit/store", help="cache folder")
    b.add_argument("--dry-run", action="store_true", help="print the estimate; no API calls")
    b.add_argument("--json", action="store_true", help="print one JSON object")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from evalkit.runtime import BudgetExceeded

    args = _parser().parse_args(argv)
    try:
        cases = load_pqa_l()
        if args.command == "verify":
            probe = load_set()
            problems = probe_problems(cases, probe)
            if problems:
                raise ProbeError("; ".join(problems))
            print(
                f"probe set matches DAT-06: {len(probe.kept)} kept, {len(probe.rejected)} "
                f"rejected, spot-check {probe.provenance.spot_check.status}"
            )
            return 0
        prices = load_prices()
        if args.dry_run:
            k = default_max_rejects(args.n) if args.max_rejects is None else args.max_rejects
            est = estimate_build(cases, prices, n=args.n, seed=args.seed, max_rejects=k)
            payload = {
                "dry_run": True,
                "n": args.n,
                "seed": args.seed,
                "max_rejects": k,
                "estimate_usd": round(est, 4),
                "max_cost_usd": args.max_cost,
            }
            text = (
                f"paraphrase probe: {args.n} questions (seed {args.seed}); estimate "
                f"${est:.2f} upper bound (worst-case checks, up to {k} replacement draws); "
                f"cap ${args.max_cost:.2f} (no API calls made)"
            )
            print(json.dumps(payload, sort_keys=True) if args.json else text)
            return 0
        adapters = _live_adapters({_provider(PARAPHRASE_MODEL), _provider(CHECK_MODEL)}, prices)
        result = asyncio.run(
            build_probe(
                cases,
                adapters,
                prices,
                Store(Path(args.store)),
                n=args.n,
                seed=args.seed,
                max_cost_usd=args.max_cost,
                max_rejects=args.max_rejects,
                echo=lambda s: print(s, file=sys.stderr),
            )
        )
        write_probe(Path(args.out), result)
        msg = (
            f"kept {len(result.kept)}, rejected {len(result.rejected)}, spent "
            f"${result.spent_usd:.4f} ({result.cache_hits} cached) → {args.out}; "
            "owner spot-check pending"
        )
        print(
            json.dumps(
                {
                    "kept": len(result.kept),
                    "rejected": len(result.rejected),
                    "spent_usd": round(result.spent_usd, 6),
                    "out": args.out,
                }
            )
            if args.json
            else msg
        )
        return 0
    except BudgetExceeded as exc:
        print(
            f"stopped at the cost cap ({exc}); finished calls are cached: rerun with a "
            "higher --max-cost",
            file=sys.stderr,
        )
        return 3
    except (ProbeError, DatasetError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
