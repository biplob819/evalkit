"""DSC-02: the import audit lists every EvalKit name a downstream checkout imports and
classes it against the public contract, without importing the downstream code (D-61)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import downstream_imports as di


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text), encoding="utf-8")


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _write(
        root,
        "src/proj/a.py",
        """
        raise SystemExit("importing downstream code is forbidden")
        from evalkit.scorers import sql_match
        from evalkit.adapters.base import SystemAdapter
        from evalkit.cost import call_cost, load_prices
        from evalkit.stats import _quantile
        import evalkit.runtime
        import os
        """,
    )
    _write(root, "src/proj/b.py", "from evalkit.cost import call_cost\n")
    _write(root, "tests/test_uses.py", "from evalkit.schemas import Case\n")
    _write(root, "tests/test_plain.py", "import json\n")
    _write(root, "scripts/tool.py", "from evalkit import __version__\n")
    _write(root, "src/proj/broken.py", "from evalkit.cost import (\n")
    _write(root, ".venv/lib/site.py", "from evalkit.pipeline import judge\n")
    _write(root, "tests/venv-old/lib/site.py", "from evalkit.pipeline import judge\n")
    _write(root, "tests/venv-old/pyvenv.cfg", "home = /usr\n")
    _write(root, "tests/devenv/test_dev.py", "from evalkit import scorers\n")
    _write(root, "tests/conftest.py", "from evalkit.schemas import Record\n")
    _write(root, "notes/other.py", "from evalkit.pipeline import judge\n")  # outside scanned dirs
    return root


def _by_key(report: di.Report) -> dict[tuple[str, str], di.Use]:
    return {(u.module, u.name): u for u in report.uses}


def test_every_import_is_listed_and_classed(tmp_path):
    report = di.audit(_checkout(tmp_path))
    uses = _by_key(report)
    assert uses[("evalkit.scorers", "sql_match")].kind == "public"
    assert uses[("evalkit", "__version__")].kind == "public"
    assert uses[("evalkit.schemas", "Case")].kind == "public"
    elsewhere = uses[("evalkit.adapters.base", "SystemAdapter")]
    assert elsewhere.kind == "public-elsewhere" and elsewhere.public_at == "evalkit.adapters"
    assert uses[("evalkit.cost", "call_cost")].kind == "internal"
    assert uses[("evalkit.cost", "call_cost")].files == 2
    assert uses[("evalkit.cost", "load_prices")].files == 1
    assert uses[("evalkit.stats", "_quantile")].kind == "private"
    assert uses[("evalkit.runtime", "*")].kind == "internal"
    assert ("os", "*") not in uses
    assert uses[("evalkit", "scorers")].kind == "public", "a public module imported whole"


def test_virtual_envs_and_unscanned_dirs_are_skipped(tmp_path):
    report = di.audit(_checkout(tmp_path))
    assert ("evalkit.pipeline", "judge") not in _by_key(report)
    assert all(".venv" not in f and "venv-old" not in f for f in report.files_scanned)
    assert "tests/devenv/test_dev.py" in report.test_files, "a folder named like venv is scanned"


def test_unparsable_file_is_reported_not_fatal(tmp_path):
    report = di.audit(_checkout(tmp_path))
    assert report.unparsable == ["src/proj/broken.py"]
    assert ("evalkit.cost", "call_cost") in _by_key(report)


def test_tests_flag_lists_only_evalkit_importing_tests(tmp_path, capsys):
    root = _checkout(tmp_path)
    assert di.main([str(root), "--tests"]) == 0
    assert capsys.readouterr().out.split() == ["tests/devenv/test_dev.py", "tests/test_uses.py"]


def test_json_is_stable_and_sorted(tmp_path, capsys):
    root = _checkout(tmp_path)
    di.main([str(root), "--json"])
    first = capsys.readouterr().out
    di.main([str(root), "--json"])
    assert capsys.readouterr().out == first
    data = json.loads(first)
    keys = [(u["module"], u["name"]) for u in data["uses"]]
    assert keys == sorted(keys)
    assert data["counts"] == {"internal": 3, "private": 1, "public": 5, "public-elsewhere": 1}
    assert data["test_files"] == ["tests/devenv/test_dev.py", "tests/test_uses.py"]


def test_text_output_groups_by_class(tmp_path, capsys):
    di.main([str(_checkout(tmp_path))])
    out = capsys.readouterr().out
    assert "internal" in out and "evalkit.cost.call_cost" in out
    assert "evalkit.adapters.base.SystemAdapter -> evalkit.adapters" in out
    assert "import evalkit.runtime" in out


def test_missing_checkout_exits_2(tmp_path, capsys):
    assert di.main([str(tmp_path / "nope")]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_self_test_passes():
    assert di.main(["--self-test"]) == 0


def test_classification_follows_the_public_contract():
    import check_public_api

    for module, names in check_public_api.PUBLIC.items():
        for name in names:
            assert di.classify(module, name)[0] == "public", (module, name)


def test_downstream_code_is_never_imported(tmp_path, monkeypatch):
    root = _checkout(tmp_path)
    marker = tmp_path / "ran"
    _write(root, "src/proj/side_effect.py", f"open({str(marker)!r}, 'w').write('x')\n")
    monkeypatch.syspath_prepend(str(root / "src"))
    di.audit(root)
    assert not marker.exists()
