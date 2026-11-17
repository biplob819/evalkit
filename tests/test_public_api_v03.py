"""B5.2 (PUB-01..04, D-61 rule 2, D-65): public API 0.3 promotes what two or more downstream
projects import, keeps every old path working, and gives ai-visibility a calibration API.

The downstream data is the B5.1 canary's recorded `imports_not_public` per project; the audit's
own classifier re-reads it against today's contract."""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import re
import subprocess
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import pytest

import check_public_api as api
import downstream_imports as di

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "docs" / "measurements" / "downstream-canary-2026-11-17.json"
SPEC = ROOT / "specs" / "B5.2-public-api" / "spec.md"
VERSIONING = ROOT / "docs" / "versioning.md"

# The modules this node made public, and the names it promoted for being part of another
# promoted name's contract (spec "Rule applied" 2) rather than for two projects' use.
V03_MODULES = (
    "evalkit.calibration",
    "evalkit.cost",
    "evalkit.errors",
    "evalkit.identity",
    "evalkit.retry",
    "evalkit.scorers.faithfulness",
    "evalkit.stats",
)
COMPANIONS = {
    "evalkit.scorers.faithfulness.FaithfulnessResult",
    "evalkit.scorers.faithfulness.JudgedClaim",
    "evalkit.calibration.AgreementTable",
    "evalkit.calibration.agreement_table",  # PUB-04, asked for by its owner (D-65)
    "evalkit.identity.EXTRACTOR_VERSION",
}


def _recorded() -> dict[str, set[str]]:
    """Each recorded non-public import ("module.name" or "import module") -> its projects."""
    data = json.loads(CANARY.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = defaultdict(set)
    for project, row in data["projects"].items():
        for line in row["imports_not_public"]:
            out[line.rsplit(" [", 1)[0].split(" -> ")[0]].add(project)
    return out


def _key(text: str) -> tuple[str, str]:
    if text.startswith("import "):
        return text.removeprefix("import "), "*"
    module, name = text.rsplit(".", 1)
    return module, name


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"^##+ ", text[start + len(heading) :], re.MULTILINE)
    return text[start : start + len(heading) + nxt.start()] if nxt else text[start:]


def _table_names(section: str) -> list[str]:
    return re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE)


def _spec_leftovers() -> list[str]:
    spec = SPEC.read_text(encoding="utf-8")
    return _table_names(_section(spec, "### Single-project leftovers"))


def _resolve(dotted: str):
    module, name = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module), name)


# ---- PUB-01: what two projects import is public ----------------------------------------------


def test_every_name_two_projects_import_is_now_public():
    for text, projects in _recorded().items():
        if len(projects) < 2:
            continue
        kind, _ = di.classify(*_key(text))
        assert kind in ("public", "public-elsewhere"), (text, sorted(projects), kind)


@pytest.mark.golden
def test_the_recorded_multi_project_names_are_exactly_the_promoted_set():
    shared = {t for t, p in _recorded().items() if len(p) >= 2}
    assert shared == {
        "evalkit.cost.Price",
        "evalkit.cost.call_cost",
        "evalkit.cost.estimate_tokens",
        "evalkit.cost.load_prices",
        "evalkit.pipeline.answer.SchemaError",
        "evalkit.pipeline.calibrate._Pair",
        "evalkit.pipeline.calibrate.cluster_kappa",
        "evalkit.pipeline.calibrate.kappa_from",
        "evalkit.pipeline.judge.JUDGE_PROMPT_HASH",
        "evalkit.retry.with_retries",
        "evalkit.scorers.faithfulness.FaithfulnessItem",
        "evalkit.scorers.faithfulness.faithfulness",
        "evalkit.stats.Resamples",
        "evalkit.stats.holm",
        "evalkit.stats.interval",
        "evalkit.stats.paired",
        "evalkit.stats.percentile_ms",
    }


@pytest.mark.contract
def test_versioning_lists_every_promoted_name_since_0_3_0():
    doc = VERSIONING.read_text(encoding="utf-8")
    public = _section(doc, "## What is public")
    for module in V03_MODULES:
        assert f"`{module}`" in public, module
        for name in api.PUBLIC[module]:
            assert f"`{name}`" in public, f"{module}.{name} missing from docs/versioning.md"
    assert public.count("since 0.3.0") >= len(V03_MODULES)
    for module in V03_MODULES:
        assert module in api.PUBLIC, module


def test_promoted_names_are_the_objects_evalkit_itself_uses():
    from evalkit import calibration, errors, identity
    from evalkit.pipeline import answer, extract, judge
    from evalkit.scorers import faithfulness as fth

    assert errors.SchemaError is answer.SchemaError
    assert identity.JUDGE_PROMPT_HASH == judge.JUDGE_PROMPT_HASH
    assert identity.EXTRACTOR_VERSION == extract.EXTRACTOR_VERSION
    assert fth.faithfulness.__module__ == "evalkit.scorers.faithfulness"
    assert calibration.kappa_from.__module__ == "evalkit.calibration"


@pytest.mark.safety
def test_no_single_project_name_entered_the_public_api():
    """D-61 rule 2 guards the other way too: nothing one project uses was promoted, except a
    name that is part of a promoted name's contract (spec, "Rule applied" 2)."""
    shared = {t for t, p in _recorded().items() if len(p) >= 2}
    homes = {api.PROMOTED.get(t, t) for t in shared}
    for module in V03_MODULES:
        for name in api.PUBLIC[module]:
            dotted = f"{module}.{name}"
            assert dotted in homes or dotted in COMPANIONS, f"{dotted} has one user"


# ---- PUB-02: old paths keep working ----------------------------------------------------------


def test_moved_calibration_paths_warn_and_return_the_new_objects():
    from evalkit import calibration

    assert api.MOVED, "the calibration functions moved"
    for old in sorted(api.MOVED):
        new = api.PROMOTED[old]
        module, name = old.rsplit(".", 1)
        mod = importlib.import_module(module)
        with pytest.warns(DeprecationWarning, match=re.escape(new)) as caught:
            obj = getattr(mod, name)
        assert "removed in 0.4.0" in str(caught[0].message)
        expected = calibration._Pair if name == "_Pair" else _resolve(new)
        assert obj is expected, old
    with pytest.warns(DeprecationWarning, match="evalkit.calibration.kappa_from"):
        from evalkit.pipeline.calibrate import kappa_from  # noqa: F401
    with pytest.raises(AttributeError):
        _ = importlib.import_module("evalkit.pipeline.calibrate").no_such_name


@pytest.mark.contract
def test_citewatch_pinned_shapes_still_hold():
    """ai-visibility's contract test pins these (inputs.md)."""
    with pytest.warns(DeprecationWarning):
        from evalkit.pipeline.calibrate import _Pair, cluster_kappa, kappa_from

    assert [f.name for f in dataclasses.fields(_Pair)] == ["answer", "human", "judge"]
    assert list(inspect.signature(kappa_from).parameters) == ["tp", "fn", "fp", "tn"]
    params = inspect.signature(cluster_kappa).parameters
    assert next(iter(params)) == "pairs"
    assert params["seed"].kind is inspect.Parameter.KEYWORD_ONLY
    pairs = [_Pair(answer=f"q{i % 3}", human=i % 2 == 0, judge=i % 4 != 1) for i in range(12)]
    assert cluster_kappa(pairs, seed=3).point == pytest.approx(kappa_from(6, 0, 3, 3))


def test_aliased_and_in_place_paths_import_silently_and_identically():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        for old, new in sorted(api.PROMOTED.items()):
            if old in api.MOVED:
                continue
            assert _resolve(old) is _resolve(new) or _resolve(old) == _resolve(new), old
        for module in ("evalkit.cost", "evalkit.retry", "evalkit.stats"):
            mod = importlib.import_module(module)
            for name in api.PUBLIC[module]:
                assert getattr(mod, name).__module__ == module
        from evalkit.pipeline.calibrate import _allocate, _opaque  # course-assistant, unchanged

        assert callable(_allocate) and callable(_opaque)


_IMPORT_ALL = """
import importlib, pkgutil, warnings
warnings.filterwarnings("error", message=r"evalkit\\..* is deprecated", category=DeprecationWarning)
import evalkit
for m in pkgutil.walk_packages(evalkit.__path__, "evalkit."):
    try:
        importlib.import_module(m.name)
    except ModuleNotFoundError as exc:  # an optional extra that is not installed
        if (exc.name or "").startswith("evalkit"):
            raise
print("ok")
"""


@pytest.mark.safety
def test_evalkit_never_imports_its_own_deprecated_paths():
    out = subprocess.run(
        [sys.executable, "-c", _IMPORT_ALL], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr[-2000:]


# ---- PUB-03: the audit reports only the single-project leftovers ------------------------------


def test_reclassified_canary_leftovers_equal_the_spec_list():
    recorded = _recorded()
    left = {t for t in recorded if di.classify(*_key(t))[0] in ("internal", "private")}
    spec = _spec_leftovers()
    assert len(spec) == len(set(spec)), "duplicate rows in the spec's leftover table"
    assert left == set(spec)
    assert all(len(recorded[t]) == 1 for t in left), "a leftover has two users: promote it"


@pytest.mark.contract
def test_the_audit_maps_promoted_paths_to_their_public_home():
    assert di.classify("evalkit.pipeline.calibrate", "_Pair") == (
        "public-elsewhere",
        "evalkit.calibration.CalibrationPair",
    )
    assert di.classify("evalkit.pipeline.calibrate", "cluster_kappa") == (
        "public-elsewhere",
        "evalkit.calibration",
    )
    assert di.classify("evalkit.pipeline.answer", "SchemaError") == (
        "public-elsewhere",
        "evalkit.errors",
    )
    assert di.classify("evalkit.cost", "call_cost") == ("public", "")
    assert di.classify("evalkit.scorers.faithfulness", "faithfulness") == ("public", "")
    assert di.classify("evalkit.scorers", "faithfulness") == ("public", "")  # the module
    assert di.classify("evalkit.cost", "price_for") == ("internal", "")
    assert di.classify("evalkit.pipeline.calibrate", "_allocate") == ("private", "")


@pytest.mark.golden
def test_recorded_canary_counts_after_promotion():
    data = json.loads(CANARY.read_text(encoding="utf-8"))
    counts = {}
    for project, row in data["projects"].items():
        kinds = [
            di.classify(*_key(t.rsplit(" [", 1)[0].split(" -> ")[0]))[0]
            for t in row["imports_not_public"]
        ]
        counts[project] = {
            k: kinds.count(k) for k in ("public", "public-elsewhere", "internal", "private")
        }
    assert counts == {
        "agentic-analytics": {"public": 9, "public-elsewhere": 0, "internal": 4, "private": 0},
        "ai-visibility": {"public": 8, "public-elsewhere": 3, "internal": 10, "private": 0},
        "assay-pipeline": {"public": 0, "public-elsewhere": 0, "internal": 0, "private": 0},
        "course-assistant": {"public": 10, "public-elsewhere": 14, "internal": 26, "private": 2},
        "fraud-review-bedrock": {"public": 2, "public-elsewhere": 0, "internal": 0, "private": 0},
        "gemma-grounded-health": {"public": 0, "public-elsewhere": 5, "internal": 13, "private": 0},
    }


def test_every_public_elsewhere_hint_resolves():
    for text in sorted(_recorded()):
        module, name = _key(text)
        kind, at = di.classify(module, name)
        if kind != "public-elsewhere":
            continue
        target = f"{at}.{name}" if at in api.PUBLIC else at
        assert _resolve(target) is not None, text
        home, attr = target.rsplit(".", 1)
        assert attr in api.PUBLIC[home], text


@pytest.mark.safety
def test_every_private_name_seen_downstream_has_a_replacement_or_reason():
    spec = SPEC.read_text(encoding="utf-8")
    table = _section(spec, "### Private names seen downstream")
    rows = dict(re.findall(r"^\| `([^`]+)` \| (.+) \|$", table, re.MULTILINE))
    private = {t for t in _recorded() if _key(t)[1].startswith("_")}
    asked = {"evalkit.pipeline.answer._extract_json", "_atomic_write", "evalkit.stats._quantile"}
    for name in private | asked:
        assert name in rows and len(rows[name]) >= 20, name
    doc = VERSIONING.read_text(encoding="utf-8")
    migration = _section(doc, "## 0.2 → 0.3 migration")
    for text in _spec_leftovers():
        shown = text.removeprefix("import ")
        assert f"`{shown}`" in migration or f"`{shown.rsplit('.', 1)[-1]}`" in migration, text


# ---- PUB-04: the public calibration API -------------------------------------------------------


def _table_pairs(cls):
    """tp=3, fn=2, fp=1, tn=4 over four clusters (citations grouped by answer)."""
    rows = [(True, True)] * 3 + [(True, False)] * 2 + [(False, True)] + [(False, False)] * 4
    return [cls(f"a{i % 4}", h, j) for i, (h, j) in enumerate(rows)]


def test_agreement_table_counts_rates_and_kappa():
    from evalkit.calibration import CalibrationPair, agreement_table, kappa_from

    t = agreement_table(_table_pairs(CalibrationPair))
    assert (t.tp, t.fn, t.fp, t.tn, t.n, t.clusters) == (3, 2, 1, 4, 10, 4)
    assert t.agreement == pytest.approx(0.7)
    assert t.strictness == pytest.approx(2 / 5)  # fn / (tp + fn)
    assert t.leniency == pytest.approx(1 / 5)  # fp / (fp + tn)
    assert t.kappa == pytest.approx(kappa_from(3, 2, 1, 4))
    assert t.owner_constant is False


@pytest.mark.contract
def test_calibration_pair_and_agreement_table_shapes():
    from evalkit import calibration as C

    assert [f.name for f in dataclasses.fields(C.CalibrationPair)] == ["cluster", "human", "judge"]
    p = C.CalibrationPair("answer-1", True, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.human = False  # type: ignore[misc]
    assert [f.name for f in dataclasses.fields(C.AgreementTable)] == [
        "tp",
        "fn",
        "fp",
        "tn",
        "n",
        "clusters",
        "agreement",
        "strictness",
        "leniency",
        "kappa",
        "owner_constant",
    ]
    assert sorted(C.__all__) == sorted(api.PUBLIC["evalkit.calibration"])
    legacy = C.agreement_table(_table_pairs(C._Pair))
    assert legacy == C.agreement_table(_table_pairs(C.CalibrationPair))


def _golden_pairs(cls):
    pairs = []
    for a in range(12):
        for k in range(a % 4 + 1):
            human = (a + k) % 3 != 0
            judge = human if (a * 7 + k) % 5 else not human
            pairs.append(cls(f"q{a}:m", human, judge))
    return pairs


@pytest.mark.golden
def test_cluster_kappa_matches_values_recorded_before_the_move():
    """Recorded from evalkit.pipeline.calibrate.cluster_kappa at 78fd982 (0.2 code)."""
    from evalkit.calibration import CalibrationPair, _Pair, agreement_table, cluster_kappa

    for cls in (CalibrationPair, _Pair):
        pairs = _golden_pairs(cls)
        t = agreement_table(pairs)
        assert (t.tp, t.fn, t.fp, t.tn) == (16, 4, 2, 8)
        assert t.kappa == 0.5714285714285715
        a = cluster_kappa(pairs, seed=0, n_resamples=500)
        assert (a.point, a.lo, a.hi) == (0.5714285714285715, 0.3019262192056309, 0.7926734462327183)
        b = cluster_kappa(pairs, seed=7, n_resamples=500)
        assert (b.lo, b.hi) == (0.30663441745574305, 0.7879192073170729)


def test_cluster_kappa_is_seeded_and_works_at_any_unit():
    from evalkit.calibration import CalibrationPair, cluster_kappa

    pairs = _golden_pairs(CalibrationPair)
    assert cluster_kappa(pairs, seed=5, n_resamples=300) == cluster_kappa(
        pairs, seed=5, n_resamples=300
    )
    # The same labels at three units. Within an answer the judge is right or wrong on every
    # claim, so claims are not independent: clustering by answer must widen the interval.
    rows = [(a, k % 2 == 0, (k % 2 == 0) == (a % 3 != 0)) for a in range(10) for k in range(5)]
    by_claim = [CalibrationPair(f"a{a}:c{k}", h, j) for k, (a, h, j) in enumerate(rows)]
    by_answer = [CalibrationPair(f"a{a}", h, j) for a, h, j in rows]
    one = [CalibrationPair("all", h, j) for _, h, j in rows]
    c = cluster_kappa(by_claim, seed=5, n_resamples=300)
    a = cluster_kappa(by_answer, seed=5, n_resamples=300)
    o = cluster_kappa(one, seed=5, n_resamples=300)
    assert c.point == a.point == o.point
    assert (c.hi - c.lo) < (a.hi - a.lo), "clustering widens the interval"
    assert o.lo == o.point == o.hi, "a single cluster cannot be resampled"
    with pytest.raises(ValueError):
        cluster_kappa([], seed=0)


@pytest.mark.safety
def test_owner_constant_flags_an_uninformative_kappa():
    from evalkit.calibration import CalibrationPair, agreement_table

    same = [CalibrationPair(f"a{i}", True, i % 3 != 0) for i in range(9)]
    t = agreement_table(same)
    assert t.owner_constant is True and t.kappa == 0.0
    assert t.leniency is None, "no human 'unsupported' labels: leniency is undefined, not 0"
    assert t.strictness == pytest.approx(3 / 9)
    empty = agreement_table([])
    assert (empty.n, empty.clusters, empty.owner_constant) == (0, 0, False)
    assert (empty.agreement, empty.strictness, empty.leniency, empty.kappa) == (None,) * 4
