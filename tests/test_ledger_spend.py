"""B5.7 spend ledger with a cap alarm (LED-06), across the five proof dimensions.

course-assistant's case is its OWNER D-37/D-64: a $150 monthly cap with the alarm at 0.9 ($135).
EvalKit's own ledger starts with 22 legacy lines that are kept byte for byte and sealed by the
first chained line (OWNER D-73). No network, no keys, no `.env`.
"""

from __future__ import annotations

import builtins
import inspect
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evalkit import cli, smoke
from evalkit import ledger as L
from evalkit.ledger._io import canonical_json, chain_state, legacy_seal, sha256_text
from tests.ledger_helpers import (  # noqa: F401
    EVALKIT_SPEND,
    ROOT,
    commit,
    no_network,
    repo,
)

LEGACY = EVALKIT_SPEND.read_text(encoding="utf-8").splitlines()[:22]


@pytest.fixture(autouse=True)
def _offline(no_network):  # noqa: F811
    yield


def _course_assistant(path: Path, amounts: list[float], month: str = "2026-11") -> Path:
    """course-assistant's per-call ledger lines (its src/course_assistant/llm/ledger.py)."""
    lines = []
    for i, usd in enumerate(amounts):
        lines.append(
            json.dumps(
                {
                    "at": f"{month}-{1 + i % 27:02d}T10:00:00+00:00",
                    "purpose": "answer",
                    "model": "m",
                    "tokens_in": 100,
                    "tokens_cached": 0,
                    "tokens_out": 50,
                    "file_search_calls": 0,
                    "tokens_cache_write": 0,
                    "tokens_reasoning": 0,
                    "cost_usd": usd,
                    "run_id": "",
                    "priced": True,
                },
                sort_keys=True,
            )
        )
    path.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    return path


def _sealed_copy(tmp: Path) -> Path:
    """EvalKit's ledger as committed: 22 legacy lines and the line that seals them."""
    p = tmp / "spend-ledger.jsonl"
    p.write_bytes(EVALKIT_SPEND.read_bytes())
    return p


# ---- logical -------------------------------------------------------------------------------------


def test_course_assistant_cap_150_with_the_alarm_at_0_9(tmp_path, capsys):
    """logical: $150 monthly cap, alarm fraction 0.9: $134.99 ok, $135 alarm (exit 0, printed),
    $150 cap (exit 3)."""
    want = {134.99: ("ok", 0), 135.0: ("alarm", 0), 149.99: ("alarm", 0), 150.0: ("cap", 3)}
    for total, (status, code) in want.items():
        path = _course_assistant(tmp_path / f"l{total}.jsonl", [total - 34.99, 20.0, 14.99])
        got = L.check_spend(
            path,
            cap=150,
            alarm_fraction=0.9,
            field_map={"spent_usd": "cost_usd"},
            now="2026-11-28T00:00:00Z",
        )
        assert (got.status, got.exit_code) == (status, code), total
        assert got.alarm_at_usd == 135.0 and got.chain == "unchained"
        argv = [
            "spend",
            "check",
            "--spend-ledger",
            str(path),
            "--cap",
            "150",
            "--alarm-fraction",
            "0.9",
            "--window",
            "total",
            "--map",
            "spent_usd=cost_usd",
        ]
        assert cli.main(argv) == code
        out = capsys.readouterr().out
        assert ("ALARM" in out) == (status == "alarm") and ("CAP" in out) == (status == "cap")
    last = _course_assistant(tmp_path / "last.jsonl", [10.0])
    default = L.check_spend(last, cap=150, field_map={"spent_usd": "cost_usd"}, window="total")
    assert default.alarm_fraction == 0.8 and default.alarm_at_usd == 120.0


def test_an_estimate_that_would_cross_the_cap_exits_3(tmp_path):
    """logical: --add-estimate: spend plus the estimate over the cap is `cap` (exit 3), so a
    script can refuse to start the run; reaching exactly the cap with it is an alarm."""
    path = _course_assistant(tmp_path / "l.jsonl", [100.0])
    fm = {"spent_usd": "cost_usd"}
    assert (
        L.check_spend(path, cap=150, add_estimate=50.01, field_map=fm, window="total").exit_code
        == 3
    )
    exact = L.check_spend(path, cap=150, add_estimate=50.0, field_map=fm, window="total")
    assert exact.status == "alarm" and exact.exit_code == 0
    assert (
        L.check_spend(path, cap=150, add_estimate=10, field_map=fm, window="total").status == "ok"
    )
    assert (
        cli.main(
            [
                "spend",
                "check",
                "--spend-ledger",
                str(path),
                "--cap",
                "150",
                "--add-estimate",
                "60",
                "--window",
                "total",
                "--map",
                "spent_usd=cost_usd",
            ]
        )
        == 3
    )


def test_windows_split_on_utc_dates(tmp_path):
    """logical: `day` and `month` are UTC; a time with a zone is converted before it is placed;
    a bare date (assay-pipeline) is a UTC date."""
    path = tmp_path / "l.jsonl"
    rows = [
        {"at": "2026-11-30T23:30:00-02:00", "spent_usd": 1.0},  # 2026-12-01 01:30 UTC
        {"at": "2026-11-30T23:30:00+00:00", "spent_usd": 2.0},
        {"at": "2026-12-01", "spent_usd": 4.0},
        {"at": "2026-12-01T00:00:00+05:30", "spent_usd": 8.0},  # 2026-11-30 18:30 UTC
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    now = "2026-12-01T12:00:00Z"
    assert L.check_spend(path, cap=100, window="day", now=now).spent_usd == 5.0
    assert L.check_spend(path, cap=100, window="month", now=now).spent_usd == 5.0
    assert L.check_spend(path, cap=100, window="day", now="2026-11-30T01:00:00Z").spent_usd == 10.0
    assert L.check_spend(path, cap=100, window="month", now="2026-11-15T00:00:00Z").spent_usd == 10
    assert L.check_spend(path, cap=100, window="total").spent_usd == 15.0
    assert L.check_spend(path, cap=100, window="month", now=now).window_start == "2026-12-01"


# ---- contract ------------------------------------------------------------------------------------


@pytest.mark.contract
def test_spend_line_shape_and_cli(tmp_path, capsys):
    """contract: a `spend` line holds the spec §7 fields, chained; `evalkit spend add|check`."""
    path = tmp_path / "spend.jsonl"
    line = L.record_spend(
        path,
        spend_kind="rejudge",
        spent_usd=1.23456789,
        project="evalkit",
        run_id="r-1",
        model="m",
        n_cases=12,
        note="n",
        detail={"judges": ["a"]},
        at="2026-11-17T00:00:00Z",
    )
    assert set(line) == {
        "kind",
        "schema",
        "at",
        "project",
        "spend_kind",
        "spent_usd",
        "run_id",
        "model",
        "n_cases",
        "note",
        "detail",
        "prev",
    }
    assert line["kind"] == "spend" and line["schema"] == "ledger-1"
    assert line["spent_usd"] == 1.234568 and line["prev"] == "0" * 64
    for bad in (-1, float("nan"), True, "3"):
        with pytest.raises(L.LedgerError):
            L.record_spend(path, spend_kind="x", spent_usd=bad, project="evalkit")  # type: ignore[arg-type]
    with pytest.raises(L.LedgerError, match="one project"):
        L.record_spend(path, spend_kind="x", spent_usd=1, project="other")
    assert (
        cli.main(
            [
                "spend",
                "add",
                "--kind",
                "smoke",
                "--usd",
                "0.5",
                "--spend-ledger",
                str(path),
                "--json",
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)
    assert added["entry"]["project"] == "evalkit" and added["running_total_usd"] == 1.734568
    assert (
        cli.main(
            [
                "spend",
                "check",
                "--spend-ledger",
                str(path),
                "--cap",
                "10",
                "--window",
                "total",
                "--json",
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    assert status["chain"] == "chained" and status["spent_usd"] == 1.734568
    assert set(status) >= {
        "status",
        "window",
        "spent_usd",
        "estimate_usd",
        "total_usd",
        "cap_usd",
        "alarm_fraction",
        "alarm_at_usd",
        "exit_code",
    }
    assert cli.main(["spend", "check", "--spend-ledger", str(tmp_path / "none"), "--cap", "1"]) == 2
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"at": "2026-11-01", "spent_usd": "lots"}\n', encoding="utf-8")
    assert cli.main(["spend", "check", "--spend-ledger", str(broken), "--cap", "1"]) == 2


@pytest.mark.contract
def test_map_reads_course_assistant_and_assay_pipeline_shapes(tmp_path):
    """contract: other projects' ledgers are read unchanged through a field map."""
    ca = _course_assistant(tmp_path / "ca.jsonl", [10.0, 2.5])
    got = L.check_spend(
        ca, cap=150, field_map={"spent_usd": "cost_usd"}, now="2026-11-20T00:00:00Z"
    )
    assert got.spent_usd == 12.5 and got.lines == 2 and got.chain == "unchained"
    assay = tmp_path / "assay.jsonl"
    assay.write_text(
        '{"date": "2026-11-15", "node": "N3", "run": "main", "model": "m", "calls": 40, '
        '"usd": 4.2}\n'
        '{"date": "2026-10-30", "node": "N2", "run": "pilot", "model": "m", "calls": 9, '
        '"usd": 1.1}\n',
        encoding="utf-8",
    )
    got = L.check_spend(
        assay, cap=25, field_map={"spent_usd": "usd", "at": "date"}, now="2026-11-20T00:00:00Z"
    )
    assert got.spent_usd == 4.2 and got.lines_in_window == 1
    assert (
        cli.main(
            [
                "spend",
                "check",
                "--spend-ledger",
                str(assay),
                "--cap",
                "25",
                "--window",
                "total",
                "--map",
                "spent_usd=usd",
                "at=date",
            ]
        )
        == 0
    )
    with pytest.raises(L.LedgerError):
        L.check_spend(assay, cap=25, field_map={"cost": "usd"})


@pytest.mark.contract
def test_smoke_record_spend_keeps_its_signature_and_return_value(tmp_path):
    """contract: `smoke.record_spend(kind, spent_usd, detail, ledger)` is unchanged and still
    returns the running total over every line; it now writes a chained, sealed line."""
    sig = inspect.signature(smoke.record_spend)
    assert list(sig.parameters) == ["kind", "spent_usd", "detail", "ledger"]
    assert sig.parameters["ledger"].default == smoke.LEDGER == EVALKIT_SPEND
    path = _sealed_copy(tmp_path)
    legacy_total = round(sum(json.loads(x)["spent_usd"] for x in LEGACY), 6)
    assert legacy_total == 31.557859  # the figure docs/case-study.md cites
    total = smoke.record_spend(
        "planted_check", 0.0773425, {"judges": ["a", "b"], "run_id": "r"}, path
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert total == round(sum(json.loads(x)["spent_usd"] for x in lines if x), 6)
    row = json.loads(lines[-1])
    assert row["spent_usd"] == round(0.0773425, 6)
    assert row["spend_kind"] == "planted_check" and row["detail"] == {"judges": ["a", "b"]}
    assert row["run_id"] == "r" and row["project"] == "evalkit"
    assert L.verify_ledger(path) == []


# ---- golden --------------------------------------------------------------------------------------


@pytest.mark.golden
def test_evalkits_legacy_ledger_is_kept_and_sealed_by_its_first_chained_line(tmp_path):
    """golden: EvalKit's 22 legacy lines are kept byte for byte; the first chained line names
    the last one's sha256 as prev and seals all 22; the repo's ledger verifies sealed."""
    problems, state, n_legacy = chain_state(EVALKIT_SPEND)
    assert (problems, state, n_legacy) == ([], "sealed", 22)
    assert L.verify_ledger(EVALKIT_SPEND) == []
    lines = EVALKIT_SPEND.read_text(encoding="utf-8").splitlines()
    seal = json.loads(lines[22])
    assert seal["prev"] == sha256_text(LEGACY[-1])
    assert (
        seal["sealed"] == legacy_seal(LEGACY) == {"lines": 22, "sha256": seal["sealed"]["sha256"]}
    )
    assert seal["spent_usd"] == 0.0 and seal["spend_kind"] == "ledger_seal"
    body = {k: v for k, v in seal.items() if k not in ("prev", "sealed")}
    sealed = {**body, "sealed": legacy_seal(LEGACY), "prev": sha256_text(LEGACY[-1])}
    assert canonical_json(sealed) == lines[22]


# ---- repro ---------------------------------------------------------------------------------------

_WRITER = """
import sys, time
from pathlib import Path
from evalkit import ledger as L
path, go, tag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
while not go.exists():
    time.sleep(0.001)
for i in range(25):
    L.record_spend(path, spend_kind=tag, spent_usd=0.01, project="evalkit", note="x" * 300)
"""


def test_concurrent_writers_never_lose_or_tear_a_line(tmp_path):
    """repro: two processes appending at once leave every line, whole and chained."""
    path = _sealed_copy(tmp_path)
    go = tmp_path / "go"
    procs = [
        subprocess.Popen([sys.executable, "-c", _WRITER, str(path), str(go), tag])
        for tag in ("a", "b")
    ]
    go.write_text("go", encoding="utf-8")
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 23 + 50
    rows = [json.loads(x) for x in lines]
    assert sorted(r.get("spend_kind") for r in rows[23:]) == ["a"] * 25 + ["b"] * 25
    assert L.verify_ledger(path) == []
    assert L.check_spend(path, cap=100, window="total").spent_usd == round(31.557859 + 0.5, 6)


# ---- safety --------------------------------------------------------------------------------------


@pytest.mark.safety
@pytest.mark.parametrize("line", [0, 10, 21])
def test_editing_a_legacy_line_breaks_the_seal(tmp_path, line):
    """safety: once sealed, an edit to any legacy line (the last one, or any before it) breaks
    the seal; spend check refuses the ledger (exit 2)."""
    path = _sealed_copy(tmp_path)
    L.record_spend(path, spend_kind="smoke", spent_usd=0.1, project="evalkit")
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[line])
    row["spent_usd"] = round(row["spent_usd"] / 2, 6)
    lines[line] = json.dumps(row, sort_keys=True)
    path.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    problems = L.verify_ledger(path)
    assert any("does not seal" in p for p in problems)
    with pytest.raises(L.LedgerError, match="broken"):
        L.check_spend(path, cap=100)
    with pytest.raises(L.LedgerError, match="broken"):
        L.record_spend(path, spend_kind="smoke", spent_usd=0.1, project="evalkit")


@pytest.mark.safety
def test_spend_commands_read_no_secrets(tmp_path, monkeypatch, capsys):
    """safety: no `.env`, key or environment secret is read or printed by the spend commands."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-NEVER-READ-THIS\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-NEVER-PRINT")
    opened: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    def io_spy(file, *a, **k):
        opened.append(str(file))
        return real_io_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(io, "open", io_spy)
    ledger = tmp_path / "s.jsonl"
    assert (
        cli.main(
            [
                "spend",
                "add",
                "--kind",
                "k",
                "--usd",
                "1",
                "--project",
                "p",
                "--spend-ledger",
                str(ledger),
            ]
        )
        == 0
    )
    assert cli.main(["spend", "check", "--cap", "5", "--spend-ledger", str(ledger)]) == 0
    out = capsys.readouterr()
    assert not any(Path(f).name == ".env" for f in opened)
    assert "NEVER" not in out.out + out.err + ledger.read_text(encoding="utf-8")
    assert str(ROOT) not in ledger.read_text(encoding="utf-8")


# ---- verifier findings ---------------------------------------------------------------------------


@pytest.mark.safety
def test_cutting_the_whole_chained_part_fails_the_chain_check(tmp_path, capsys):
    """safety: EvalKit's ledger cut back to its 22 legacy lines (every chained line dropped) is
    not a legacy file waiting for its seal: that prefix is a recorded checkpoint
    (`KNOWN_SEALS`), so verify reports it, spend check refuses it (exit 2) and no line can be
    appended (that would re-seal a shorter history). Another project's unsealed legacy file is
    still read as it is."""
    path = _sealed_copy(tmp_path)
    L.record_spend(path, spend_kind="smoke", spent_usd=0.5, project="evalkit")
    path.write_text("".join(x + "\n" for x in LEGACY), encoding="utf-8")
    assert any("chained part" in p for p in L.verify_ledger(path))
    assert cli.main(["history", "verify", str(path), "--chain-only"]) == 1
    with pytest.raises(L.LedgerError, match="broken") as exc:
        L.check_spend(path, cap=100, window="total")
    assert exc.value.code == 2
    before = path.read_bytes()
    with pytest.raises(L.LedgerError, match="broken"):
        L.record_spend(path, spend_kind="smoke", spent_usd=0.1, project="evalkit")
    assert path.read_bytes() == before
    capsys.readouterr()
    assert cli.main(["spend", "check", "--cap", "100", "--spend-ledger", str(path)]) == 2
    other = tmp_path / "other.jsonl"
    other.write_text("".join(x + "\n" for x in LEGACY[:21]), encoding="utf-8")
    assert L.verify_ledger(other) == []
    assert L.check_spend(other, cap=100, window="total").chain == "unchained"


@pytest.mark.safety
def test_tail_truncation_is_caught_by_the_append_only_check_and_verify_says_so(tmp_path, capsys):
    """safety: dropping the last chained lines leaves a valid chain, so the defence there is
    B5.14's append-only check against the repository: `history verify --rev` catches it, and
    every verify reports for each ledger whether that check ran."""
    root = repo(tmp_path)
    path = root / "spend-ledger.jsonl"
    path.write_bytes(EVALKIT_SPEND.read_bytes())
    for _ in range(3):
        L.record_spend(path, spend_kind="smoke", spent_usd=0.1, project="evalkit")
    base = commit(root, "spend")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:-2]), encoding="utf-8")
    assert L.verify_ledger(path) == []  # the chain alone cannot see a cut tail
    capsys.readouterr()
    assert cli.main(["history", "verify", str(path), "--chain-only", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["append_only"][str(path)].startswith("not checked")
    assert cli.main(["history", "verify", str(path), "--chain-only"]) == 0
    assert "append-only: not checked" in capsys.readouterr().out
    assert cli.main(["history", "verify", str(path), "--rev", base, "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["append_only"][str(path)].startswith("checked")
    assert out["ledgers"][str(path)]
