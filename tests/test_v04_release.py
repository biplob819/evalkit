"""B5.10: v0.4.0 release candidate and the 0.3 -> 0.4 migration guide (V04-01, V04-02).

V04-01: the version agrees everywhere; the changelog opens with a consolidated 0.4.0 section;
value_check and its companions are public again with the D-74 WITHHELD guard emptied (its
check kept); D-61 rule 2 is applied to the 2026-11-18 canary record; docs/versioning.md has
0.4.0 additions and a 0.3 -> 0.4 migration; docs/downstream.md has a "0.3 -> 0.4" part per
registered project naming every non-public import in that record, and every replacement it
names imports and is public. The release-candidate canary (V04-01) and the release record
(V04-02) are recorded by the lead and the owner; their tests skip until the files exist.
No network, no spend.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
import tomllib
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

import evalkit

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "downstream.md"
VERSIONING = ROOT / "docs" / "versioning.md"
PLUGINS = ROOT / "docs" / "plugins.md"
MEASURE = ROOT / "docs" / "measurements"
CANARY = MEASURE / "downstream-canary-2026-11-18.json"
VERSION = "0.4.0"
SUBSECTION = "### 0.3 → 0.4"

_spec = importlib.util.spec_from_file_location(
    "check_public_api", ROOT / "scripts" / "check_public_api.py"
)
assert _spec and _spec.loader
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)
PUBLIC: dict[str, list[str]] = _api.PUBLIC

sys.path.insert(0, str(ROOT / "scripts"))
import downstream_canary  # noqa: E402
import downstream_imports as di  # noqa: E402

REGISTRY = downstream_canary.load_registry()
DOTTED = re.compile(r"^(?:import )?evalkit(?:\.\w+)*$")
STATUSES_OK = {"pass", "known-drift", "no-tests"}

VALUE_CHECK = (
    "value_check",
    "value_check_scorer",
    "value_check_shares",
    "ValuePhrase",
    "ValueCheckShares",
)
# Public homes first public in 0.4.0 under D-61 rule 2, and the ones promoted only as part of
# such a name's contract (returned or raised by it): the B5.2 rule.
PROMOTED_HOMES = {
    "evalkit.cost.TokenAssumptions",
    "evalkit.stats.interval_from",
    "evalkit.identity.JUDGE_RUBRIC",
    "evalkit.runtime.Executor",
    "evalkit.runtime.Store",
    "evalkit.providers.provider_for",
}
CONTRACT_COMPANIONS = {
    "evalkit.cost.CostEstimate": "returned by estimate_faithfulness_cost",
    "evalkit.cost.CostLine": "CostEstimate.lines",
    "evalkit.runtime.BudgetExceeded": "raised by evalkit.runtime.Executor",
}


def _resolve(dotted: str) -> object:
    """Import the longest module prefix of `dotted`, then walk the attributes after it."""
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj: object = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(dotted)


def _is_public(dotted: str) -> bool:
    if dotted in PUBLIC:
        return True
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:cut])
        if module in PUBLIC:
            return parts[cut] in PUBLIC[module]
    return False


def _key(text: str) -> tuple[str, str]:
    if text.startswith("import "):
        return text.removeprefix("import "), "*"
    module, name = text.rsplit(".", 1)
    return module, name


def _home(text: str) -> str | None:
    """Where a recorded import is public today, or None if it is internal or private."""
    module, name = _key(text)
    kind, at = di.classify(module, name)
    if kind == "public":
        return text
    if kind == "public-elsewhere":
        return f"{at}.{name}" if at in PUBLIC else at
    return None


def _recorded() -> dict[str, set[str]]:
    """Each non-public import in the 2026-11-18 record -> the projects that import it."""
    rec = json.loads(CANARY.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = defaultdict(set)
    for project, row in rec["projects"].items():
        for entry in row.get("imports_not_public", []):
            out[entry.split(" -> ")[0].split(" [")[0]].add(project)
    return out


# ---- V04-01 version and changelog -------------------------------------------------------------


def test_version_is_0_4_0_in_pyproject_package_and_lock():
    """At release the tree was 0.4.0; a later 0.4.x patch keeps the 0.4 line (test_v041_release)."""
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["version"] == evalkit.__version__
    assert Version(VERSION) <= Version(proj["version"]) < Version("0.5.0")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f'name = "evalkit-harness"\nversion = "{proj["version"]}"' in lock


def _changelog_section() -> str:
    text = (ROOT / "changelog.md").read_text(encoding="utf-8")
    return text.split(f"## {VERSION}\n", 1)[1].split("\n## ", 1)[0]


def test_changelog_opens_with_a_consolidated_0_4_0_section():
    text = (ROOT / "changelog.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.M)
    released = [h for h in headings if re.fullmatch(r"\d+\.\d+\.\d+", h)]
    i = released.index(VERSION)  # 0.4.x patches may come first
    assert released[i + 1] == "0.3.0" and all(h.startswith("0.4.") for h in released[:i]), released
    section = _changelog_section()
    assert "[RELEASE] [B5.10]" in section
    for needed in (
        "structured-facts",  # value_check as the D-77 check
        "D-77",
        "D-74",
        "evalkit.ledger",  # B5.7
        "evalkit.drift",
        "evalkit history",
        "evalkit spend",
        "evalkit.heldout",  # no longer provisional
        "evalkit.paraphrase",
        "evalkit.integrity",
        "evalkit.security",
        "equivalences",  # B5.15
        "claims_from_texts",  # D-76
        "estimate_faithfulness_cost",
        "--claims-per-answer",
        "two_level_interval",  # clustered intervals
        "cluster=",
        "cluster_trials=True",
        "judge_errors",  # D-78
        "missing_verdicts",
        "exit 4",
        "id",  # id-shaped label values
        "run-folder detection",
        "--out",
        "full history",  # the canary clones
        "0.5.0",  # moved calibration paths kept
    ):
        assert needed in section, needed


def test_changelog_names_every_name_made_public_in_0_4_0():
    section = _changelog_section()
    for name in (*VALUE_CHECK, "ClaimsAssumption", "measured_claims"):
        assert re.search(rf"`(?:evalkit\.\w+\.)?{name}`", section), name
    for dotted in PROMOTED_HOMES | set(CONTRACT_COMPANIONS):
        assert dotted.rsplit(".", 1)[1] in section, dotted


def test_value_check_is_described_as_the_strict_facts_check_not_the_withheld_one():
    section = _changelog_section()
    line = next(x for x in section.split("\n- ") if "`value_check`" in x and "[B5.16]" in x)
    assert "field is value" in line and "unverifiable" in line and "facts line" in line


# ---- V04-01 value_check is public again --------------------------------------------------------


def test_value_check_names_import_from_evalkit_scorers_and_are_public():
    import evalkit.scorers as scorers
    from evalkit.scorers import evidence

    for name in VALUE_CHECK:
        assert name in scorers.__all__ and name in PUBLIC["evalkit.scorers"], name
        assert getattr(scorers, name) is getattr(evidence, name), name
        exec(f"from evalkit.scorers import {name}", {})


def test_the_withheld_guard_is_empty_but_kept():
    """D-74's entries are gone; the mechanism stays for the next name a release holds back."""
    assert _api.WITHHELD == {}
    assert _api.missing() == []
    source = (ROOT / "scripts" / "check_public_api.py").read_text(encoding="utf-8")
    assert "for module, names in WITHHELD.items()" in source


def test_public_value_check_is_the_strict_structured_facts_check():
    from evalkit.scorers import value_check, value_check_shares

    record = {"amount": 812.4, "hour": 3, "risk_score": 0.83}
    units: dict[str, Any] = {"amount": "currency", "hour": "hour", "risk_score": "fraction"}
    results = [
        value_check(r, record, units=units)
        for r in (
            "A large late-night purchase.\nFacts: Amount is 812.40. Hour is 3.",  # facts line
            "Facts: Amount is 900.00. Hour is 3.",  # a false fact
            "A purchase of 812.40 at 3 a.m., well above usual.",  # prose: never verified
        )
    ]
    assert [r.details["outcome"] for r in results] == ["verified", "contradicted", "unverifiable"]
    shares = value_check_shares(results)
    assert (shares.verified, shares.contradicted, shares.unverifiable) == (1, 1, 1)


# ---- V04-01 D-61 rule 2 on the 2026-11-18 record -----------------------------------------------


def test_every_name_two_projects_import_is_public():
    for text, projects in _recorded().items():
        if len(projects) >= 2:
            assert _home(text), (text, sorted(projects))


def test_each_name_promoted_in_0_4_0_has_two_users_or_is_in_a_promoted_contract():
    users: dict[str, set[str]] = defaultdict(set)
    for text, projects in _recorded().items():
        home = _home(text)
        if home:
            users[home] |= projects
    for home in PROMOTED_HOMES:
        assert _is_public(home) and _resolve(home) is not None, home
        assert len(users[home]) >= 2, (home, sorted(users[home]))
    for home, why in CONTRACT_COMPANIONS.items():
        assert _is_public(home) and _resolve(home) is not None, (home, why)


def test_promoted_names_are_the_objects_evalkit_itself_uses_and_old_paths_are_silent():
    from evalkit import cost, identity, providers, runtime, stats
    from evalkit.pipeline import generate, judge
    from evalkit.runtime import executor, store
    from evalkit.scorers import faithfulness

    assert providers.provider_for is generate.provider_for
    assert identity.JUDGE_RUBRIC is judge.JUDGE_RUBRIC
    assert runtime.Store is store.Store and runtime.Executor is executor.Executor
    assert runtime.BudgetExceeded is executor.BudgetExceeded
    assert stats.interval_from.__module__ == "evalkit.stats"
    hints = faithfulness.estimate_faithfulness_cost.__annotations__
    assert hints["return"] == "CostEstimate" and "TokenAssumptions" in hints["assumptions"]
    assert {"CostEstimate", "CostLine", "TokenAssumptions"} <= set(cost.__all__)
    assert sorted(runtime.__all__) == ["BudgetExceeded", "Executor", "Store"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for old in (
            "evalkit.pipeline.generate.provider_for",
            "evalkit.pipeline.judge.JUDGE_RUBRIC",
            "evalkit.runtime.store.Store",
            "evalkit.runtime.executor.BudgetExceeded",
            "evalkit.runtime.Node",  # still importable, just not public
        ):
            assert _resolve(old) is not None, old


def test_moved_calibration_paths_are_kept_until_0_5_0():
    """Due in 0.4.0; two registered projects still import them in the 2026-11-18 record."""
    users = {t: p for t, p in _recorded().items() if t in _api.MOVED}
    assert set(users) == set(_api.MOVED)
    assert {p for ps in users.values() for p in ps} == {"ai-visibility", "course-assistant"}
    with pytest.warns(DeprecationWarning, match="removed in 0.5.0"):
        _resolve("evalkit.pipeline.calibrate.kappa_from")


# ---- V04-01 the 0.3 -> 0.4 guide ---------------------------------------------------------------


def _subsections() -> dict[str, str]:
    """Each project's "0.3 → 0.4" part of docs/downstream.md."""
    text = GUIDE.read_text(encoding="utf-8")
    out = {}
    for part in re.split(r"^## ", text, flags=re.M)[1:]:
        name = part.split("\n", 1)[0].strip()
        if SUBSECTION in part:
            out[name] = part.split(SUBSECTION, 1)[1]
    return out


def _rows(section: str) -> list[tuple[str, str, str]]:
    out = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith(("`evalkit", "`import evalkit")):
            out.append((cells[0].strip("`"), cells[1], cells[2]))
    return out


def _replacements() -> list[tuple[str, str, str]]:
    out = []
    for project, body in _subsections().items():
        for today, _kind, replace in _rows(body):
            for span in re.findall(r"`([^`]+)`", replace):
                if DOTTED.match(span):
                    out.append((project, today, span.removeprefix("import ")))
    return out


def test_every_registered_project_has_a_0_4_part_with_its_requirement():
    subs = _subsections()
    for row in REGISTRY:
        assert row.project in subs, row.project
        req = re.search(r"\*\*0\.4\.0 requirement\*\*: `([^`]+)`", subs[row.project])
        assert req, row.project
        parsed = Requirement(req.group(1))
        assert parsed.name == "evalkit-harness"
        assert Version(VERSION) in parsed.specifier, req.group(1)
        assert not parsed.specifier.contains("0.3.0") and not parsed.specifier.contains("0.5.0")
        assert "**2026-11-18 canary**" in subs[row.project]


def test_the_0_4_guide_names_every_non_public_import_in_the_2026_11_18_record():
    rec = json.loads(CANARY.read_text(encoding="utf-8"))
    subs = _subsections()
    for project, row in rec["projects"].items():
        recorded = {e.split(" -> ")[0].split(" [")[0] for e in row.get("imports_not_public", [])}
        named = {today for today, _, _ in _rows(subs[project])}
        assert recorded == named, (project, recorded ^ named)


def test_every_0_4_replacement_imports_and_is_public():
    reps = _replacements()
    assert len(reps) > 60
    for project, today, dotted in reps:
        assert _resolve(dotted) is not None, (project, today, dotted)
        assert _is_public(dotted), f"{project}: {today} -> {dotted} is not public"


def test_each_0_4_class_agrees_with_the_contract():
    """public/moved rows are public today (and point at their home); internal and private rows
    are not, and each has a single user in the record (D-61 rule 2)."""
    recorded = _recorded()
    for project, body in _subsections().items():
        for today, kind, replace in _rows(body):
            home = _home(today)
            if kind.startswith(("public", "moved")):
                assert home, (project, today, kind)
                assert f"`{home}`" in replace, (project, today, home)
            else:
                assert kind.startswith(("internal", "private")), (project, today, kind)
                assert home is None, (project, today, home)
                assert len(recorded[today]) == 1, (project, today, sorted(recorded[today]))


def test_fraud_review_bedrocks_18_names_are_each_classified_under_rule_2():
    rows = _rows(_subsections()["fraud-review-bedrock"])
    assert len(rows) == 18
    internal = sorted(t for t, kind, _ in rows if kind.startswith("internal"))
    assert internal == [
        "evalkit.cost.estimate_run_cost",
        "evalkit.env.KEYS",
        "evalkit.env.missing_keys",
        "evalkit.pipeline.extract.FAITHFULNESS_EXTRACTOR_PROMPT",
        "evalkit.pipeline.extract.FAITHFULNESS_QUESTION_RULE",
        "evalkit.run.make_adapters",
        "evalkit.stats.ALPHA",
    ]
    for today, kind, replace in rows:
        assert "rule 2" in kind or kind == "moved" or "Executor" in kind, (today, kind)
        assert replace.strip() and replace != "keep", today


def test_behaviour_changes_and_capabilities_each_project_asked_for_are_named():
    subs = _subsections()
    needed = {
        "agentic-analytics": ("equivalences=", "cluster=", "evalkit.ledger.record_run"),
        "ai-visibility": ("estimate_faithfulness_cost", "0.5.0"),
        "assay-pipeline": ("--out DIR", "gate.json", "compare.json"),
        "course-assistant": ("evalkit.ledger.check_spend", "evalkit spend check", "--map"),
        "fraud-review-bedrock": (
            "estimate_faithfulness_cost",
            "D-76",
            "Facts:",
            "evalkit.scorers.value_check",
            "evalkit.drift.psi",
        ),
        "gemma-grounded-health": ("D-78", "judge_errors", "missing_verdicts", "exit 4"),
        "respectful-offers": ("evalkit.drift.check", "severity", "id-shaped"),
    }
    seen = 0
    for project, words in needed.items():
        body = subs[project]
        assert "Behaviour changes in 0.4 that affect it" in body, project
        assert "Capabilities it asked for, in 0.4.0" in body, project
        for word in words:
            assert word in body, (project, word)
        caps = body.split("Capabilities it asked for, in 0.4.0", 1)[1]
        for span in re.findall(r"`([^`]+)`", caps):
            if DOTTED.match(span):
                seen += 1
                assert _resolve(span) is not None, (project, span)
                assert _is_public(span), f"{project}: capability {span} is not public"
    assert seen > 40


# ---- V04-01 versioning and plugins docs --------------------------------------------------------


def _doc_section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"^## ", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + nxt.start()] if nxt else text[start:]


def test_versioning_resolves_the_provisional_note_and_lists_0_4_0():
    doc = VERSIONING.read_text(encoding="utf-8")
    assert "on main ahead of the release" not in doc
    additions = _doc_section(doc, "## 0.4.0 additions\n")
    for needed in (*VALUE_CHECK, "evalkit.ledger", "evalkit.heldout", "judge_errors"):
        assert needed in additions, needed
    v03 = _doc_section(doc, "## 0.3.0 additions\n")
    assert "Provisional in 0.3.0, resolved in 0.4.0" in v03
    assert "covered by the usual one-minor-version deprecation window" in v03
    public = _doc_section(doc, "## What is public\n")
    for home in PROMOTED_HOMES | set(CONTRACT_COMPANIONS):
        module, name = home.rsplit(".", 1)
        assert f"`{module}`" in public and f"`{name}`" in public, home


def test_versioning_has_a_0_3_to_0_4_migration():
    doc = VERSIONING.read_text(encoding="utf-8")
    mig = _doc_section(doc, "## 0.3 → 0.4 migration\n")
    for needed in (
        "D-76",
        "D-78",
        "exits 4",
        "Id-shaped label values",
        "run-folder detection",
        "`--out` may name a folder",
        "`sql_match` details",
        "`value_check` is public and strict",
        "0.5.0",
    ):
        assert needed in mig, needed
    homes = set()
    for line in mig.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[1].startswith("`evalkit"):
            homes |= {s for s in re.findall(r"`([^`]+)`", cells[1]) if DOTTED.match(s)}
    assert homes >= PROMOTED_HOMES | {"evalkit.runtime.BudgetExceeded"}
    for home in homes:
        assert _is_public(home) and _resolve(home) is not None, home


def test_plugins_doc_headings_say_0_4_0_and_import_from_the_public_path():
    text = PLUGINS.read_text(encoding="utf-8")
    assert "### Agent and decision scorers (0.3.0; value_check 0.4.0)" in text
    assert "#### value_check: a structured-facts check (0.4.0, D-74, D-77)" in text
    assert "#### value_check: three outcomes (D-72)\n" in text
    assert "development, not public" not in text and "internal until 0.4.0" not in text
    assert "from evalkit.scorers.evidence import" not in text
    assert "from evalkit.scorers import value_check, value_check_shares" in text


# ---- V04-01 release-candidate canary (the lead records it after B5.7 is green) ----------------


def _rc4() -> Path | None:
    found = sorted(MEASURE.glob("downstream-canary-*-rc4.json"))
    return found[-1] if found else None


def test_rc_canary_covers_every_registered_project():
    path = _rc4()
    if path is None:
        pytest.skip("RC canary not recorded yet (the lead runs it after B5.7 is green)")
    rec = json.loads(path.read_text(encoding="utf-8"))
    tested = {r.project for r in REGISTRY if r.tests in {"auto", "all"}}
    assert set(rec["projects"]) == tested
    assert rec["spend_usd"] == 0 and re.fullmatch(r"[0-9a-f]{40}", rec["evalkit_commit"])
    assert rec["evalkit_src_matches_commit"] is True
    assert rec["evalkit_version"] == VERSION
    assert "--all" in rec["command"]
    for name, p in rec["projects"].items():
        assert p["status"] in STATUSES_OK, (name, p["status"])
        if p["status"] != "pass":
            assert p.get("finding"), f"{name}: {p['status']} needs a finding"
        if p["status"] == "known-drift":
            row = next(r for r in REGISTRY if r.project == name)
            # a project may rename a known test after the record (fraud-review-bedrock renamed
            # its pin test when it moved to the v0.4.0 release); map old names to new
            renamed = {
                "tests/test_doctor.py::test_evalkit_is_pinned_to_an_exact_commit_and_imports": (
                    "tests/test_doctor.py::test_evalkit_is_pinned_to_an_exact_release_and_imports"
                ),
            }
            failures = {renamed.get(f, f) for f in p["failures"]}
            # the known ids the run itself used (a project may later fix them and leave the
            # registry); fall back to today's registry for records written before that field
            allowed = {renamed.get(k, k) for k in p.get("known", [])} | set(row.known_failures())
            assert failures <= allowed, name


# ---- V04-02 release record (after the owner approves the tag and the publish) -----------------


def test_the_v0_4_0_release_record_matches_the_tag_the_tree_and_pypi_files():
    found = sorted(MEASURE.glob("release-v0.4.0-*.json"))
    if not found:
        pytest.skip("v0.4.0 not released yet (V04-02 is the owner's step)")
    rec = json.loads(found[-1].read_text(encoding="utf-8"))
    assert rec["tag"] == f"v{VERSION}" == f"v{rec['version']}"
    assert re.fullmatch(r"[0-9a-f]{40}", rec["commit"])
    assert Version(evalkit.__version__) >= Version(rec["version"])
    assert "owner" in rec["approved_by"]
    names = sorted(f["filename"] for f in rec["files"])
    assert names == [
        f"evalkit_harness-{VERSION}-py3-none-any.whl",
        f"evalkit_harness-{VERSION}.tar.gz",
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in rec["files"])
    assert rec["spend_usd"] == 0
