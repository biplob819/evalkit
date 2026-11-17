"""B5.4: v0.3.0 release candidate and the downstream migration guide (V03-01, V03-02).

V03-01: the version agrees everywhere, the changelog opens with a consolidated 0.3.0 section,
value_check is withheld from the public API (OWNER D-74), and the release-candidate canary
covers every registered project with a passing, explained or test-less status.
V03-02: docs/downstream.md has a section per registered project with its current pin and the
0.3.0 requirement line; it names every non-public import recorded by any canary run, and every
replacement it names imports and is public. No network, no spend.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

import evalkit

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "downstream.md"
MEASURE = ROOT / "docs" / "measurements"
RC = MEASURE / "downstream-canary-2026-11-17-rc.json"
VERSION = "0.3.0"

_spec = importlib.util.spec_from_file_location(
    "check_public_api", ROOT / "scripts" / "check_public_api.py"
)
assert _spec and _spec.loader
_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api)
PUBLIC: dict[str, list[str]] = _api.PUBLIC
WITHHELD: dict[str, list[str]] = _api.WITHHELD

sys.path.insert(0, str(ROOT / "scripts"))
import downstream_canary  # noqa: E402

REGISTRY = downstream_canary.load_registry()
DOTTED = re.compile(r"^(?:import )?evalkit(?:\.\w+)*$")
STATUSES_OK = {"pass", "known-drift", "no-tests"}


# ---- V03-01 version consistency ---------------------------------------------------------------


def test_version_is_0_3_0_in_pyproject_package_and_lock():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["version"] == evalkit.__version__ == VERSION
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert f'name = "evalkit-harness"\nversion = "{VERSION}"' in lock


def test_changelog_opens_with_a_consolidated_0_3_0_section():
    text = (ROOT / "changelog.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings[0] == VERSION, headings[:3]
    section = text.split(f"## {VERSION}\n", 1)[1].split("\n## ", 1)[0]
    assert "[RELEASE] [B5.4]" in section
    for needed in (
        "evalkit gate",  # B5.3
        "evalkit compare",  # B5.6
        "citation_support",  # B5.12
        "rule_compliance",  # B5.13
        "evalkit.calibration",  # B5.2
        "efficiency",  # D-70 summary.json
        "one-sided",  # D-70 gate bounds
        "MIN_CASES",  # compare
        "value_check",  # D-74, named as withheld
        "D-74",
    ):
        assert needed in section, needed
    # the dated entries below are kept: the next heading is a date
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", headings[1])


def test_value_check_is_withheld_from_the_public_api():
    """OWNER D-74: not exported from `evalkit.scorers`, not in the contract, still importable
    from its internal module for EvalKit's own tests; render_evidence stays public."""
    import evalkit.scorers as scorers
    from evalkit.scorers import evidence

    withheld = set(WITHHELD["evalkit.scorers"])
    assert withheld == {
        "value_check",
        "value_check_scorer",
        "value_check_shares",
        "ValuePhrase",
        "ValueCheckShares",
    }
    assert not withheld & set(scorers.__all__)
    assert not withheld & set(PUBLIC["evalkit.scorers"])
    assert not withheld & set(vars(scorers))
    for name in withheld:
        with pytest.raises(ImportError):
            exec(f"from evalkit.scorers import {name}", {})
        assert hasattr(evidence, name)
    assert "render_evidence" in scorers.__all__ and "render_evidence" in PUBLIC["evalkit.scorers"]


def test_the_contract_check_fails_if_a_withheld_name_is_exported(monkeypatch):
    import evalkit.scorers as scorers
    from evalkit.scorers.evidence import value_check

    assert _api.missing() == []
    monkeypatch.setattr(scorers, "value_check", value_check, raising=False)
    assert _api.missing() == ["evalkit.scorers.value_check (withheld, D-74)"]


def test_plugins_doc_marks_value_check_as_coming_in_0_4_0():
    text = (ROOT / "docs" / "plugins.md").read_text(encoding="utf-8")
    assert "#### value_check: coming in 0.4.0 (D-74)" in text
    assert "from evalkit.scorers import value_check" not in text.replace(
        "`from evalkit.scorers import value_check` fails in 0.3.0", ""
    )


# ---- V03-01 release-candidate canary ----------------------------------------------------------


def _rc() -> dict:
    return json.loads(RC.read_text(encoding="utf-8"))


def test_rc_canary_covers_every_registered_project_with_tests():
    rec = _rc()
    tested = {r.project for r in REGISTRY if r.tests in {"auto", "all"}}
    assert set(rec["projects"]) == tested
    assert rec["spend_usd"] == 0 and re.fullmatch(r"[0-9a-f]{40}", rec["evalkit_commit"])
    assert rec["evalkit_src_matches_commit"] is True
    assert rec["evalkit_version"] == VERSION
    assert "--all" in rec["command"]


def _withheld_import_break(p: dict) -> bool:
    """A failure made only of collection errors on a name D-74 withheld from evalkit.scorers."""
    names = "|".join(WITHHELD["evalkit.scorers"])
    hits = re.findall(rf"cannot import name '(?:{names})' from 'evalkit\.scorers'", p["detail"])
    c = p["counts"]
    return bool(hits) and c["failed"] == 0 and c["errors"] == len(p["failures"]) > 0


def test_rc_canary_statuses_are_ok_or_a_d74_break_named_in_the_guide():
    """Every project passes, drifts only by its registered known ids, or has no tests. The one
    allowed exception is a project broken only by D-74's withheld value_check names: it must
    say so in its finding and in its section of docs/downstream.md (the owner's call)."""
    rec = _rc()
    sections = _sections()
    for name, p in rec["projects"].items():
        if p["status"] != "pass":
            assert p.get("finding"), f"{name}: {p['status']} needs a finding"
        if p["status"] == "known-drift":
            row = next(r for r in REGISTRY if r.project == name)
            assert set(p["failures"]) <= set(row.known_failures())
        if p["status"] not in STATUSES_OK:
            assert p["status"] == "fail" and _withheld_import_break(p), (name, p["status"])
            assert p["finding"].startswith("D-74"), name
            assert "fail, caused by 0.3.0 (D-74)" in sections[name], name


# ---- V03-02 migration guide -------------------------------------------------------------------


def _sections() -> dict[str, str]:
    text = GUIDE.read_text(encoding="utf-8")
    parts = re.split(r"^## ", text, flags=re.M)[1:]
    return {p.split("\n", 1)[0].strip(): p for p in parts}


def _rows(section: str) -> list[tuple[str, str, str]]:
    """(import today, class, replace with) from the import table of one project section."""
    out = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith(("`evalkit", "`import evalkit")):
            out.append((cells[0].strip("`"), cells[1], cells[2]))
    return out


def _all_rows() -> dict[str, list[tuple[str, str, str]]]:
    return {name: _rows(body) for name, body in _sections().items()}


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
    if dotted in PUBLIC:  # a public module
        return True
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        module = ".".join(parts[:cut])
        if module in PUBLIC:
            return parts[cut] in PUBLIC[module]
    return False


def _replacements() -> list[tuple[str, str, str]]:
    out = []
    for project, rows in _all_rows().items():
        for today, _kind, replace in rows:
            for span in re.findall(r"`([^`]+)`", replace):
                if DOTTED.match(span):
                    out.append((project, today, span.removeprefix("import ")))
    return out


def _recorded_imports() -> dict[str, set[str]]:
    """Every non-public import any committed canary record holds, per project."""
    out: dict[str, set[str]] = {}
    for path in sorted(MEASURE.glob("downstream-canary-*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        for name, p in rec["projects"].items():
            for entry in p.get("imports_not_public", []):
                out.setdefault(name, set()).add(entry.split(" -> ")[0].split(" [")[0])
    return out


def test_the_guide_has_a_section_per_registered_project_with_pin_and_requirement():
    sections = _sections()
    for row in REGISTRY:
        assert row.project in sections, row.project
        body = sections[row.project]
        assert "**Current pin**" in body and "**0.3.0 requirement**" in body
        if row.pin != "-":
            assert row.pin in body, f"{row.project}: registry pin not named"
            req = re.search(r"\*\*0\.3\.0 requirement\*\*: `([^`]+)`", body)
            assert req, row.project
            parsed = Requirement(req.group(1))
            assert parsed.name == "evalkit-harness"
            assert Version(VERSION) in parsed.specifier, req.group(1)
            assert not parsed.specifier.contains("0.2.0") and not parsed.specifier.contains("0.4.0")


def test_the_guide_names_every_non_public_import_in_the_canary_records():
    rows = _all_rows()
    for project, imports in _recorded_imports().items():
        named = {today for today, _, _ in rows.get(project, [])}
        assert imports <= named, f"{project}: not in docs/downstream.md: {imports - named}"


def test_every_replacement_in_the_guide_imports_and_is_public():
    reps = _replacements()
    assert len(reps) > 40  # the guide really names replacements
    for project, today, dotted in reps:
        assert _resolve(dotted) is not None, (project, today, dotted)
        assert _is_public(dotted), f"{project}: {today} -> {dotted} is not public"


def test_moved_and_promoted_paths_point_at_their_public_homes():
    """Each PROMOTED path in the guide is replaced by exactly its public home."""
    for project, rows in _all_rows().items():
        for today, _, replace in rows:
            if today in _api.PROMOTED:
                assert f"`{_api.PROMOTED[today]}`" in replace, (project, today)


def test_withheld_value_check_is_never_offered_as_a_replacement():
    """Safety (D-74): the guide never tells a project to use a withheld or internal name."""
    for project, today, dotted in _replacements():
        name = dotted.rsplit(".", 1)[-1]
        assert name not in WITHHELD["evalkit.scorers"], (project, today, dotted)
        assert not dotted.startswith("evalkit.scorers.evidence."), (project, today, dotted)


def test_behaviour_changes_and_offers_are_named():
    sections = _sections()
    assert "systems" in sections["gemma-grounded-health"]
    assert "test_spec_covers_every_run_config_field" in sections["gemma-grounded-health"]
    offers = sections["respectful-offers"]
    assert "equal-width" in offers and "D-73" in offers
    assert "D-74" in sections["fraud-review-bedrock"]
    text = GUIDE.read_text(encoding="utf-8")
    for needed in ("D-70", "efficiency", "MIN_CASES", "sql_match", "example_gold_row"):
        assert needed in text, needed


def test_every_capability_named_for_a_project_imports_and_is_public():
    """The capabilities each project asked for are named by their public names."""
    seen = 0
    for row in REGISTRY:
        body = _sections()[row.project]
        assert "Capabilities it asked for" in body, row.project
        caps = body.split("Capabilities it asked for", 1)[1]
        for span in re.findall(r"`([^`]+)`", caps):
            if DOTTED.match(span):
                seen += 1
                assert _resolve(span) is not None, (row.project, span)
                assert _is_public(span), f"{row.project}: capability {span} is not public"
    assert seen > 30
