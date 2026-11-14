"""B2.5: the docs' commands run, the privacy statement matches the code, and the release,
CI-example and community files say what the criteria require. No network, no spend."""

from __future__ import annotations

import ast
import re
import shlex
import time
from pathlib import Path

import pytest
import yaml

from evalkit import cli

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
    ROOT / "CONTRIBUTING.md",
]
FENCE = re.compile(r"```(?:bash|shell|sh)\n(.*?)```", re.S)


def doc_commands() -> list[tuple[str, str]]:
    out = []
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        for block in FENCE.findall(text):
            for line in block.splitlines():
                for part in line.split("  #")[0].split("&&"):
                    cmd = part.strip()
                    if cmd.startswith("evalkit"):
                        out.append((path.name, cmd))
    return out


COMMANDS = doc_commands()


def test_the_docs_show_the_key_commands():
    shown = {c for _, c in COMMANDS}
    for needed in ("evalkit init", "evalkit doctor --offline", "evalkit report --example"):
        assert needed in shown
    assert any("--dry-run" in c for c in shown)
    assert {"quickstart.md", "writing-a-task.md", "cost.md"} <= {p for p, _ in COMMANDS}


@pytest.mark.parametrize(("doc", "cmd"), COMMANDS, ids=[f"{d}:{c}" for d, c in COMMANDS])
def test_every_documented_command_parses(doc, cmd):
    argv = shlex.split(cmd)[1:]
    parser = cli.build_parser()
    if argv == ["--version"]:
        return
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # pragma: no cover - the failure message is the point
        pytest.fail(f"{doc}: `{cmd}` is not a valid command ({exc})")
    assert getattr(args, "func", None), f"{doc}: `{cmd}` names no subcommand"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def refuse(*a, **k):
        raise OSError("network disabled in test")

    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    return tmp_path


def test_the_quickstart_runs_without_keys_or_network_in_well_under_five_minutes(
    project, capsys, monkeypatch
):
    from evalkit.datasets import scifact as S
    from tests.datasets.test_scifact import tarball

    monkeypatch.setattr(
        "evalkit.run.load_scifact", lambda: S.parse_scifact(S.slim_from_tarball(tarball()))
    )
    # the quickstart adds keys (step 4) before doctor; fake ones, never sent anywhere
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-quickstart-NEVER-PRINT")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-quickstart-NEVER-PRINT")
    t0 = time.perf_counter()
    quick = [
        c
        for d, c in COMMANDS
        if d == "quickstart.md" and c != "evalkit --version" and "pubmedqa" not in c
    ]
    ran = []
    for cmd in quick:
        argv = shlex.split(cmd)[1:]
        if argv[0] == "run" and "--dry-run" not in argv:
            continue  # the paid step: covered by the scripted end-to-end tests
        if argv[0] == "run":  # the synthetic SciFact archive has three claims
            argv = [*argv[: argv.index("--n") + 1], "3", *argv[argv.index("--n") + 2 :]]
        code = cli.main(argv)
        ran.append((cmd, code))
        assert code == 0, f"`{cmd}` exited {code}: {capsys.readouterr()}"
    assert (project / "evalkit-example" / "report.html").is_file()
    assert {"evalkit init", "evalkit doctor --offline", "evalkit report --example"} <= {
        c for c, _ in ran
    }
    assert any("scifact" in c for c, _ in ran) and any("my_task" in c for c, _ in ran)
    assert time.perf_counter() - t0 < 60


# ---- OSS-07 privacy ---------------------------------------------------------------------------

NETWORK_MODULES = {
    "urllib.request",
    "socket",
    "http.client",
    "httpx",
    "requests",
    "anthropic",
    "openai",
    "psycopg",
}
ALLOWED = {
    "evalkit/adapters/anthropic.py": "provider SDK",
    "evalkit/adapters/openai.py": "provider SDK",
    "evalkit/datasets/pubmedqa.py": "PubMedQA download",
    "evalkit/datasets/scifact.py": "SciFact download",
    "evalkit/onboarding.py": "doctor reachability check",
    "evalkit/demo/pubmed.py": "demo PubMed lookup",
    "evalkit/demo/page.py": "demo: visitor-key provider clients",
    "evalkit/demo/guard.py": "demo: daily spend database",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_only_the_modules_in_privacy_md_can_reach_the_network():
    src = ROOT / "src"
    offenders = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        hits = {
            m
            for m in _imports(path)
            if any(m == n or m.startswith(n + ".") for n in NETWORK_MODULES)
        }
        if hits and rel not in ALLOWED:
            offenders[rel] = sorted(hits)
    assert offenders == {}, f"network access outside PRIVACY.md: {offenders}"


def test_privacy_md_names_every_destination_and_no_telemetry():
    text = (ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    assert "no telemetry" in text.lower()
    for needed in ("Anthropic", "OpenAI", "Hugging Face", "SciFact", "doctor", "PubMed"):
        assert needed in text


# ---- OSS-05 community files -------------------------------------------------------------------


def test_community_files_exist_and_say_what_they_must():
    for name in ("CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "PRIVACY.md"):
        assert (ROOT / name).is_file(), name
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "security/advisories/new" in security and "key" in security.lower()
    for name in ("bug_report.yml", "feature_request.yml", "config.yml"):
        yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text(encoding="utf-8"))
    assert (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").is_file()


# ---- OSS-06 GitHub Action example -------------------------------------------------------------


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads `on:` as True
    return data


def test_the_action_example_fails_on_a_threshold_and_always_uploads_the_report():
    wf = _workflow(ROOT / "examples" / "github-action" / "evalkit.yml")
    steps = wf["jobs"]["evaluate"]["steps"]
    runs = [s for s in steps if "evalkit run" in s.get("run", "")]
    dry, real = runs[0], runs[-1]
    assert "--dry-run" in dry["run"] and "--dry-run" not in real["run"]
    assert "|| true" not in real["run"] and not real.get("continue-on-error")
    upload = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
    assert upload.get("if") == "always()"
    env = wf["jobs"]["evaluate"]["env"]
    assert set(env) == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    assert all(v.startswith("${{ secrets.") for v in env.values())


# ---- OSS-01 / OSS-02 / OSS-08 release ---------------------------------------------------------


def test_release_publishes_by_trusted_publishing_and_smoke_tests_every_os():
    wf = _workflow(ROOT / ".github" / "workflows" / "release.yml")
    assert wf["on"] == {"push": {"tags": ["v*"]}}
    publish = wf["jobs"]["publish"]
    assert publish["permissions"]["id-token"] == "write" and publish["environment"] == "pypi"
    text = yaml.safe_dump(publish)
    assert "--trusted-publishing always" in text and "GITHUB_REF_NAME" in text
    assert "TOKEN" not in text.replace("GITHUB_REF_NAME", "")
    smoke = wf["jobs"]["smoke"]
    assert smoke["needs"] == "publish"
    assert set(smoke["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    }
    body = yaml.safe_dump(smoke)
    assert "doctor --offline" in body and "report --example" in body


def test_versions_agree_for_the_release():
    import tomllib

    import evalkit

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["name"] == "evalkit-harness" and meta["version"] == evalkit.__version__
    assert meta["scripts"]["evalkit"] == "evalkit.cli:main"
    assert f"## {meta['version']}" in (ROOT / "changelog.md").read_text(encoding="utf-8") or (
        "[RELEASE]" in (ROOT / "changelog.md").read_text(encoding="utf-8")
    )
