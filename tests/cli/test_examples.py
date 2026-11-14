"""OSS-04: the second public task (SciFact) and the user-data templates (JSONL from `evalkit
init`, CSV) run end to end through `evalkit run` on scripted models. No network, no spend."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from evalkit import cli
from evalkit.datasets import load_csv_cases
from evalkit.datasets.local import DatasetFileError
from evalkit.schemas import TaskConfig
from tests.datasets.test_scifact import tarball
from tests.golden.test_golden_run import reply as golden_reply
from tests.pipeline.fakes import ScriptedBatchAdapter

FAKE = "fake-examples-NEVER-PRINT-1234"


def reply_with(labels: tuple[str, ...]):
    """The golden scripted models, answering with the task's own labels."""

    def reply(req):
        out = golden_reply(req)
        if req.model in ("extractor", "judge"):
            return out
        try:
            data = json.loads(out)
        except ValueError:
            return out
        data["label"] = labels[len(req.prompt) % len(labels)]
        return json.dumps(data)

    return reply


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    cli.main(["init"])
    return tmp_path


def use_fakes(monkeypatch, labels):
    from evalkit import run as R

    fake = ScriptedBatchAdapter(reply_with(labels))
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": fake, "openai": fake}
    )
    return fake


def run_json(capsys, *args: str) -> tuple[int, dict]:
    capsys.readouterr()
    code = cli.main(["run", "evalkit.yaml", *args, "--wait", "0.05", "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_scifact_runs_end_to_end(project, capsys, monkeypatch):
    from evalkit.datasets import scifact as S

    cases = S.parse_scifact(S.slim_from_tarball(tarball()))
    monkeypatch.setattr("evalkit.run.load_scifact", lambda: cases)
    fake = use_fakes(monkeypatch, ("supports", "refutes", "not enough info"))
    code, out = run_json(capsys, "--task", "scifact", "--n", "3")
    assert code in (0, 1), out
    summary = json.loads((project / out["run_dir"] / "summary.json").read_text(encoding="utf-8"))
    assert summary["task"] == "scifact" and all(m["n"] == 3 for m in summary["models"])
    prompts = [r.prompt for r in fake.requests if r.model not in ("extractor", "judge")]
    assert prompts and all("Claim:" in p for p in prompts)
    assert FAKE not in json.dumps(out)


CSV = (
    "id,question,context,label\n"
    'c1,Does the policy cover floods?,"The policy covers fire. Floods are excluded.",no\n'
    'c2,Is theft covered?,"Theft is covered up to $5,000.\nClaims need a police report.",yes\n'
    'c3,Is mould covered?,"The policy is silent on mould.",\n'
)


def csv_task(project: Path) -> Path:
    (project / "tasks" / "my_data.csv").write_text(CSV, encoding="utf-8")
    yaml_path = project / "tasks" / "my_task.yaml"
    text = yaml_path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^dataset: .*$", "dataset: my_data.csv", text)
    out = project / "tasks" / "csv_task.yaml"
    out.write_text(text, encoding="utf-8")
    return out


def test_a_csv_task_runs_end_to_end(project, capsys, monkeypatch):
    task = csv_task(project)
    use_fakes(monkeypatch, ("yes", "no", "maybe"))
    code, out = run_json(capsys, "--task", str(task), "--n", "3")
    assert code in (0, 1), out
    summary = json.loads((project / out["run_dir"] / "summary.json").read_text(encoding="utf-8"))
    assert all(m["n"] == 3 for m in summary["models"])


def _task(labels=("yes", "no", "maybe")) -> TaskConfig:
    return TaskConfig(
        name="t",
        dataset="x.csv",
        split="all",
        labels=labels,
        prompt_template="{question}",
        output_schema="answer_v1",
        scorers=("label_correct",),
    )


def test_csv_rows_become_cases_split_into_sentences(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text(CSV, encoding="utf-8")
    cases = load_csv_cases(path, _task())
    assert [c.id for c in cases] == ["c1", "c2", "c3"]
    assert cases[0].context == ("The policy covers fire.", "Floods are excluded.")
    assert cases[1].context == ("Theft is covered up to $5,000.", "Claims need a police report.")
    assert cases[0].reference_label == "no" and cases[2].reference_label is None


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("id,question\nc1,Q?\n", "missing column"),
        ("id,question,context\nc1,Q?,\n", "context must be"),
        ("id,question,context,label\nc1,Q?,Text.,perhaps\n", "not one of the task labels"),
        ("id,question,context\nc1,Q?,A.\nc1,Q?,B.\n", "duplicate id"),
        ("id,question,context\nc:1,Q?,A.\n", "without ':'"),
        ("id,question,context\n", "no cases"),
    ],
)
def test_csv_problems_name_the_file_and_line(tmp_path, text, match):
    path = tmp_path / "d.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(DatasetFileError, match=match):
        load_csv_cases(path, _task())


def test_csv_with_a_spreadsheet_bom_and_crlf_loads(tmp_path):
    path = tmp_path / "d.csv"
    path.write_bytes(("﻿" + CSV.replace("\n", "\r\n")).encode("utf-8"))
    assert len(load_csv_cases(path, _task())) == 3


def test_csv_errors_name_the_line_the_row_starts_on(tmp_path):
    text = (
        "id,question,context,label\n"
        'a,Q?,"Line one.\nLine two.\nLine three.",yes\n'
        "\n"
        "\n"
        "b,Q?,Text.,perhaps\n"
    )
    path = tmp_path / "d.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(DatasetFileError, match=r"d\.csv:7: label"):
        load_csv_cases(path, _task())


def test_a_non_utf8_file_is_a_clear_error(tmp_path):
    path = tmp_path / "d.csv"
    path.write_bytes("id,question,context\nc1,Q?,It\u2019s here.\n".encode("cp1252"))
    with pytest.raises(DatasetFileError, match=r"not UTF-8.*CSV UTF-8"):
        load_csv_cases(path, _task())
    jl = tmp_path / "d.jsonl"
    jl.write_bytes(b'{"id": "a", "question": "Q?", "context": ["It\x92s."]}\n')
    from evalkit.datasets import load_jsonl_cases

    with pytest.raises(DatasetFileError, match="not UTF-8"):
        load_jsonl_cases(jl, _task())


def test_csv_headers_any_case_and_order_extra_columns_ignored_spaced_labels(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text(
        "Notes,Label,Context,Question,ID\nignore me, Yes ,The sky is blue.,Is it blue?,7\n",
        encoding="utf-8",
    )
    (case,) = load_csv_cases(path, _task())
    assert (case.id, case.reference_label, case.context) == ("7", "yes", ("The sky is blue.",))


def test_csv_duplicate_columns_are_refused(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text("id,question,context,context\n1,Q?,A.,B.\n", encoding="utf-8")
    with pytest.raises(DatasetFileError, match="duplicate column"):
        load_csv_cases(path, _task())


def test_a_long_document_in_one_cell_loads(tmp_path):
    long = " ".join(f"Sentence number {i} is here." for i in range(10_000))  # ~300k chars
    path = tmp_path / "d.csv"
    path.write_text(f'id,question,context\n1,Q?,"{long}"\n', encoding="utf-8")
    (case,) = load_csv_cases(path, _task())
    assert len(case.context) == 10_000


def test_an_unclosed_quote_is_an_error_not_a_swallowed_row(tmp_path):
    path = tmp_path / "d.csv"
    path.write_text('id,question,context\nc1,Q?,"unterminated\nc2,Q?,B.\n', encoding="utf-8")
    with pytest.raises(DatasetFileError, match=r"d\.csv:2: not valid CSV"):
        load_csv_cases(path, _task())
