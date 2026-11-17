"""B2.5: the docs' commands run, the privacy statement matches the code, and the release,
CI-example and community files say what the criteria require. No network, no spend."""

from __future__ import annotations

import ast
import json
import re
import shlex
import time
from pathlib import Path

import pytest
import yaml

from evalkit import cli

ROOT = Path(__file__).resolve().parents[1]
NOT_DOCS = {"decisions.md", "lessons.md", "changelog.md"}  # build records, not user docs
DOCS = sorted(
    p
    for p in [
        *ROOT.glob("*.md"),
        *(ROOT / "docs").glob("*.md"),
        *(ROOT / ".github").rglob("*.md"),
        *(ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml"),
    ]
    if p.name not in NOT_DOCS
    and "PRD" not in p.name
    and "Tech Spec" not in p.name
    and "Portfolio" not in p.name
)
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)  # every fence, whatever its language
INLINE = re.compile(r"`(evalkit [^`\n]+)`")
ACTION = ROOT / "examples" / "github-action" / "evalkit.yml"


def doc_commands() -> list[tuple[str, str, str]]:
    """(doc, command, kind): every `evalkit ...` line in a fence ("block") or in inline code
    ("inline"), with `&&` chains split and trailing comments dropped."""
    out: list[tuple[str, str, str]] = []
    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        for block in FENCE.findall(text):
            for line in block.splitlines():
                for part in line.split("#")[0].split("&&"):
                    if part.strip().startswith("evalkit"):
                        out.append((path.name, part.strip(), "block"))
        for cmd in INLINE.findall(FENCE.sub("", text)):
            out.append((path.name, cmd.strip(), "inline"))
    return out


def action_commands() -> list[str]:
    wf = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    env = wf["jobs"]["evaluate"]["env"]
    cmds = []
    for step in wf["jobs"]["evaluate"]["steps"]:
        run = step.get("run", "")
        if run.startswith("evalkit "):
            for k, v in env.items():
                run = run.replace(f'"${k}"', str(v))
            cmds.append(run.strip())
    return cmds


COMMANDS = doc_commands()
# Inline mentions of a bare subcommand ("`evalkit run`") are references, not commands.
PARSEABLE = [
    (d, c)
    for d, c, kind in COMMANDS
    if kind == "block" or len(shlex.split(c)) > 2 or c.split()[1] in ("init", "doctor")
]
# Commands that cannot run unattended here, each covered by the test named.
NOT_RUN_HERE = {
    "calibrate label": "needs a person labelling: tests/pipeline/test_calibrate.py",
    "calibrate score": "needs human labels: tests/golden test_golden_calibration_end_to_end",
    "report --publish": "needs a calibrated run: tests/report/test_readme.py",
    "--version": "tests/test_cli.py",
}


def test_the_docs_show_the_key_commands():
    shown = {c for _, c, _ in COMMANDS}
    for needed in ("evalkit init", "evalkit doctor --offline", "evalkit report --example"):
        assert needed in shown
    assert any("--dry-run" in c for c in shown)
    assert {"quickstart.md", "writing-a-task.md", "cost.md", "calibrate-your-task.md"} <= {
        d for d, _, _ in COMMANDS
    }


@pytest.mark.parametrize(("doc", "cmd"), PARSEABLE, ids=[f"{d}:{c}" for d, c in PARSEABLE])
def test_every_documented_command_parses(doc, cmd):
    argv = [a if a != "RUN_DIR" else "runs/r-x" for a in shlex.split(cmd)[1:]]
    if argv == ["--version"]:
        return
    try:
        args = cli.build_parser().parse_args(argv)
    except SystemExit as exc:  # pragma: no cover - the failure message is the point
        pytest.fail(f"{doc}: `{cmd}` is not a valid command ({exc})")
    assert getattr(args, "func", None), f"{doc}: `{cmd}` names no subcommand"


def _scripted_reply(req):
    from tests.golden.test_golden_run import reply as golden

    out = golden(req)
    if req.model in ("extractor", "judge"):
        return out
    try:
        data = json.loads(out)
    except ValueError:
        return out
    labels = (
        ("supports", "refutes", "not enough info")
        if "Claim:" in req.prompt
        else (
            "yes",
            "no",
            "maybe",
        )
    )
    data["label"] = labels[len(req.prompt) % 3]
    return json.dumps(data)


def _many(cases, n: int):
    """`cases` repeated with unique ids, so documented sample sizes fit."""
    out = []
    for k in range(n):
        c = cases[k % len(cases)]
        out.append(c.model_copy(update={"id": f"{c.id}-{k}"}))
    return out


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A scaffolded project with fake keys, scripted models, synthetic public datasets and the
    network refused: every documented command can run, and nothing can be spent."""
    from evalkit import run as R
    from evalkit.datasets import parse_pqa_l
    from evalkit.datasets import scifact as S
    from tests.datasets.test_scifact import tarball
    from tests.pipeline.fakes import ScriptedBatchAdapter

    monkeypatch.chdir(tmp_path)

    def refuse(*a, **k):
        raise OSError("network disabled in test")

    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-docs-NEVER-PRINT")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-docs-NEVER-PRINT")
    mini = json.loads((ROOT / "tests" / "fixtures" / "pubmedqa_mini.json").read_text("utf-8"))
    monkeypatch.setattr(R, "load_pqa_l", lambda: _many(parse_pqa_l(mini), 250))
    sci = S.parse_scifact(S.slim_from_tarball(tarball()))
    monkeypatch.setattr(R, "load_scifact", lambda: _many(sci, 250))
    fake = ScriptedBatchAdapter(_scripted_reply)
    monkeypatch.setattr(R, "make_adapters", lambda c, p: {"anthropic": fake, "openai": fake})
    assert cli.main(["init"]) == 0  # the real scaffold, three sample cases, as a reader has it
    return tmp_path


def _biggest_run(root: Path) -> str:
    runs = [p for p in (root / "runs").glob("r-*") if (p / "summary.json").is_file()]
    return str(max(runs, key=lambda p: len((p / "records.jsonl").read_bytes())).relative_to(root))


def test_every_documented_command_runs(project, capsys):
    """OSS-03: each command the docs and the CI example show runs in a scaffolded project
    (scripted models, no keys, no network) and exits as documented."""
    t0 = time.perf_counter()
    seen: set[str] = set()
    todo = [c for _, c in PARSEABLE] + action_commands()
    todo.sort(key=lambda c: "calibrate" in c)  # calibration works on a finished run
    ran = []
    calibration_run: list[bool] = []
    for cmd in todo:
        if cmd in seen:
            continue
        seen.add(cmd)
        if any(k in cmd for k in NOT_RUN_HERE):
            continue
        argv = shlex.split(cmd)[1:]
        if "RUN_DIR" in argv:
            if not calibration_run:  # the calibration doc's step 1: a run of 100+ answers
                assert cli.main(["run", "evalkit.yaml", "--task", "pubmedqa", "--n", "30"]) in (
                    0,
                    1,
                )
                capsys.readouterr()
                calibration_run.append(True)
            argv = [_biggest_run(project) if a == "RUN_DIR" else a for a in argv]
        code = cli.main(argv)
        out = capsys.readouterr()
        paid_run = argv[0] == "run" and "--dry-run" not in argv
        allowed = {0, 1} if paid_run else {0}  # 1 = a threshold failed, a valid outcome
        assert code in allowed, f"`{cmd}` exited {code}:\n{out.out}\n{out.err}"
        ran.append(cmd)
    assert (project / "evalkit-example" / "report.html").is_file()
    assert any("calibrate export" in c for c in ran)
    assert any("scifact" in c for c in ran) and any("pubmedqa" in c for c in ran)
    for needed in (
        "evalkit init",
        "evalkit doctor --offline",
        "evalkit report --example",
        "evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run",
        "evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3",
        "evalkit run evalkit.yaml --task scifact --n 20 --dry-run",
        "evalkit calibrate export RUN_DIR",
    ):
        assert needed in ran, needed
    assert time.perf_counter() - t0 < 120  # the quickstart is minutes, not hours


# ---- OSS-07 privacy ---------------------------------------------------------------------------

NETWORK_MODULES = {
    "urllib",  # urllib.request, `from urllib import request`
    "socket",
    "ssl",
    "http",  # http.client, http.server
    "ftplib",
    "smtplib",
    "webbrowser",
    "subprocess",  # could shell out to curl
    "httpx",
    "httpx2",
    "requests",
    "urllib3",
    "aiohttp",
    "websockets",
    "grpc",
    "anthropic",
    "openai",
    "psycopg",
    "streamlit",
    "duckdb",  # can read https:// URLs; the agent sandbox switches that off
    "multiprocessing.connection",
    "xmlrpc",
    "telnetlib",
    "poplib",
    "imaplib",
}
HARMLESS = {"urllib.parse", "urllib.error", "http.HTTPStatus"}  # no I/O
NETWORK_CALLS = {
    "open_connection",
    "start_server",
    "create_connection",
    "create_server",
    "sock_connect",
}
SHELL_CALLS = ("system", "popen", "exec", "spawn", "posix_spawn")


def _dotted(node: ast.AST) -> str:
    """`a.b.c` for an attribute chain rooted at a plain name, else ""."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    return ".".join([node.id, *reversed(parts)])


ALLOWED = {
    "evalkit/adapters/anthropic.py": "provider SDK",
    "evalkit/adapters/openai.py": "provider SDK",
    "evalkit/datasets/pubmedqa.py": "PubMedQA download",
    "evalkit/datasets/scifact.py": "SciFact download",
    "evalkit/onboarding.py": "doctor reachability check",
    "evalkit/pipeline/labeller.py": "local labelling page on 127.0.0.1",
    "evalkit/demo/pubmed.py": "demo PubMed lookup",
    "evalkit/demo/page.py": "demo: visitor-key provider clients",
    "evalkit/demo/guard.py": "demo: daily spend database",
    # DuckDB reaches the network only if a query asks it to:
    "evalkit/agents/db.py": "builds the local agent database with fixed SQL",
    "evalkit/agents/tasks.py": "checks gold answers with fixed SQL on the local database",
    "evalkit/agents/sandbox.py": "runs agent SQL with enable_external_access off",
    # B5.14: local git reads only (ls-files, show, rev-list, rev-parse, merge-base); no fetch
    "evalkit/heldout.py": "reads the local git repository for the held-out checks",
}
DYNAMIC_IMPORT_OK = {"evalkit/runtime/versions.py"}  # walks evalkit's own modules to hash them


def _network_uses(path: Path) -> set[str]:
    """Networking a module can do: imports of network libraries (any spelling), dynamic
    imports of them, and asyncio/socket-style connection calls."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            chain = _dotted(node)
            if chain:
                names.add(chain)  # e.g. urllib.request.urlopen reached via `import urllib.error`
        if isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if _dotted(f).startswith("os.") and fname.startswith(SHELL_CALLS):
                names.add(f"call:os.{fname}")
            if fname in ("exec", "eval") and isinstance(f, ast.Name):
                names.add("call:exec")
            if fname == "getattr" and len(node.args) > 1:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and arg.value in ("import_module", "__import__"):
                    names.add("dynamic-import")
            if fname in NETWORK_CALLS:
                names.add(f"call:{fname}")
            if fname in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
                else:
                    names.add("dynamic-import")
    hits = set()
    for n in names:
        if n in HARMLESS or any(n.startswith(h + ".") for h in HARMLESS):
            continue
        if n.startswith("call:") or n == "dynamic-import":
            hits.add(n)
        elif any(n == m or n.startswith(m + ".") for m in NETWORK_MODULES):
            hits.add(n.split(".")[0] if n.split(".")[0] in {"subprocess", "socket"} else n)
    return hits


def test_only_the_modules_in_privacy_md_can_reach_the_network():
    src = ROOT / "src"
    offenders = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        hits = _network_uses(path)
        if rel in DYNAMIC_IMPORT_OK:
            hits.discard("dynamic-import")
        if hits and rel not in ALLOWED:
            offenders[rel] = sorted(hits)
    assert offenders == {}, f"network access outside PRIVACY.md: {offenders}"


@pytest.mark.parametrize(
    "code",
    [
        "from urllib import request",
        "import urllib.request",
        "import http.client",
        "from http.server import HTTPServer",
        "import importlib\nimportlib.import_module('socket')",
        "import subprocess",
        "import asyncio\nasyncio.open_connection('h', 1)",
        "import ssl",
        "import urllib3",
        "import urllib.error\nurllib.request.urlopen('https://x')",
        "import os\nos.system('curl https://x')",
        "import os\nos.popen('curl https://x')",
        "import importlib\ngetattr(importlib, 'import_module')('socket')",
        "exec('import socket')",
        "import asyncio\nasyncio.get_event_loop().sock_connect(None, None)",
        "import duckdb",
        "from multiprocessing.connection import Client",
    ],
)
def test_the_privacy_scan_catches_each_way_to_reach_the_network(tmp_path, code):
    f = tmp_path / "m.py"
    f.write_text(code + "\n", encoding="utf-8")
    assert _network_uses(f), code


def test_the_privacy_scan_ignores_parsing_helpers(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("from urllib.parse import quote\nimport urllib.error\n", encoding="utf-8")
    assert _network_uses(f) == set()


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
    wf = _workflow(ACTION)
    job = wf["jobs"]["evaluate"]
    steps = job["steps"]
    runs = [s for s in steps if "evalkit run" in s.get("run", "")]
    dry, real = runs[0], runs[-1]
    assert "--dry-run" in dry["run"] and "--dry-run" not in real["run"]
    assert "|| true" not in real["run"] and not real.get("continue-on-error")
    assert job["env"]["N"] == "3" and job["env"]["TASK"] == "tasks/my_task.yaml"  # init's task
    secrets = {k: v for k, v in job["env"].items() if k.endswith("_API_KEY")}
    assert set(secrets) == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
    assert all(v.startswith("${{ secrets.") for v in secrets.values())
    assert "head.repo.full_name == github.repository" in job["if"]  # forks get no secrets
    restore = next(s for s in steps if "cache/restore" in s.get("uses", ""))
    save = next(s for s in steps if "cache/save" in s.get("uses", ""))
    assert save.get("if") == "always()"  # a failed or pending run still saves its cache
    for step in (restore, save):
        assert ".evalkit/store" in step["with"]["path"] and "runs" in step["with"]["path"]
    # a new key for every run AND every re-run attempt, so each one is saved
    assert save["with"]["key"].endswith("${{ github.run_id }}-${{ github.run_attempt }}")
    assert restore["with"]["key"] == save["with"]["key"]
    upload = next(s for s in steps if "upload-artifact" in s.get("uses", ""))
    assert upload.get("if") == "always()"
    paths = upload["with"]["path"].split()
    assert paths == ["runs/*/report.html", "runs/*/summary.json"]  # never the records


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
    body = "\n".join(step.get("run", "") for step in smoke["steps"])
    assert "doctor --offline --json" in body and "report --example" in body
    assert "evalkit-harness[anthropic,openai]==$v" in body  # doctor needs the SDKs
    assert "startswith('key')" in body  # no keys in CI: only the key checks may fail
    assert "check_public_api.py" in body  # every public name, from the published package


def test_versions_agree_for_the_release():
    import tomllib

    import evalkit

    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert meta["name"] == "evalkit-harness" and meta["version"] == evalkit.__version__
    assert meta["scripts"]["evalkit"] == "evalkit.cli:main"
    assert "[RELEASE] [B2.5] — v0.1.0" in (ROOT / "changelog.md").read_text(encoding="utf-8")
