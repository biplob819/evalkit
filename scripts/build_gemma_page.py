#!/usr/bin/env python3
"""Build and check the /gemma/ page (B5.17, GEM-03): Gemma Grounded Health's v2 main run,
rendered by EvalKit's own report renderer.

    uv run python scripts/build_gemma_page.py                 # write docs/gemma/index.html
    uv run python scripts/build_gemma_page.py --check         # rebuild, compare, content checks
    uv run python scripts/build_gemma_page.py --check --ggh ~/gemma-grounded-health
    uv run python scripts/build_gemma_page.py --self-test     # prove every check can fail

`docs/gemma/` holds GGH's committed `summary.json`, `run.json` and `charts.vl.json` byte for
byte, and `source.json`: GGH's commit, the sha256 of each copied file, and the statements the
page adds, each with the GGH file it was read from. Per-case records are never copied, so the
renderer's failure explorer is replaced by a note. `--ggh` also compares `source.json` with a
GGH checkout (its working tree). No network, no model call.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GEMMA = ROOT / "docs" / "gemma"
COPIED = ("summary.json", "run.json", "charts.vl.json")
PAGE = "index.html"
PLUGINS_URL = "https://github.com/biplob819/evalkit/blob/main/docs/plugins.md"
CASE_ID_RE = re.compile(r"pqa-\d")
LONG_NUMBER_RE = re.compile(r"(?<![\w.])\d{7,}(?![\w])")
FORBIDDEN_NAME_RE = re.compile(r"record|case", re.IGNORECASE)


class PageError(ValueError):
    """The renderer's output no longer has a place this page edits."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_source(gemma: Path) -> dict[str, Any]:
    return json.loads((gemma / "source.json").read_text(encoding="utf-8"))


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _code(text: str) -> str:
    """Escape, then show `backticked` spans as code."""
    parts = _e(text).split("`")
    return "".join(f"<code>{p}</code>" if i % 2 else p for i, p in enumerate(parts))


def _f3(v: float) -> str:
    return f"{v:.3f}"


def _aim_tables(aim: dict[str, Any]) -> str:
    rates = "".join(
        f"<tr><th scope=row>{_e(r['model'])}</th>"
        f"<td>{_f3(r['published']['citation_precision'])}</td>"
        f"<td>{_f3(r['corrected']['citation_precision'])}</td>"
        f"<td>{_f3(r['published']['aim_citation_rate'])}</td>"
        f"<td>{_f3(r['corrected']['aim_citation_rate'])}</td></tr>"
        for r in aim["rates"]
    )
    pairs = "".join(
        f"<tr><th scope=row>{_e(c['metric'])}</th><td>{_e(c['model_a'])} − {_e(c['model_b'])}</td>"
        f"<td>{_e(c['count'])}</td><td>{c['diff']['point']:+.3f} "
        f"({c['diff']['lo']:+.3f} to {c['diff']['hi']:+.3f})</td>"
        f"<td>{'separated' if c['separated'] else 'tie'}</td></tr>"
        for c in aim["comparisons"]
    )
    return (
        f"<table id=gemma-aim><caption>{_e(aim['judge'])}'s citation figures as published "
        "(the pre-registered numbers, above in this report) and under GGH's post-hoc count "
        "(“corrected”)</caption><thead><tr><th scope=col>Model</th>"
        "<th scope=col>Citation precision, published</th><th scope=col>corrected</th>"
        "<th scope=col>Aim-citation rate, published</th><th scope=col>corrected</th></tr>"
        f"</thead><tbody>{rates}</tbody></table>"
        "<table id=gemma-aim-pairs><caption>Paired differences under each count, as GGH "
        "computed them (95% intervals; “separated” after its Holm correction)</caption><thead>"
        "<tr><th scope=col>Metric</th><th scope=col>Pair</th><th scope=col>Count</th>"
        "<th scope=col>Difference</th><th scope=col>Call</th></tr></thead>"
        f"<tbody>{pairs}</tbody></table>"
    )


def gemma_card(src: dict[str, Any]) -> str:
    repo, commit = src["repo"], src["commit"]
    run = src["run"]
    run_url = f"{repo}/tree/{commit}/{run['path']}"
    roles = ", ".join(f"<code>{_e(m)}</code> ({_e(r)})" for m, r in src["models"]["roles"].items())
    j, cal, aim, links = src["judging"], src["calibration"], src["aim_rule"], src["links"]
    return (
        "<h2 id=gemma-h>About this run</h2><div class=card id=gemma>"
        f"<p>This is EvalKit's report for <a href=\"{_e(repo)}\">{_e(src['project'])}</a>'s "
        f"v2 main run, rendered by EvalKit's report renderer from the run's committed "
        f'<code>summary.json</code> and <code>run.json</code> (<a href="{_e(run_url)}">'
        f"<code>{_e(run['path'])}</code> at {_e(commit[:7])}</a>). Only aggregate figures are "
        "shown; the run's per-case records are not published here.</p><ul>"
        "<li><strong>Decision ids.</strong> Ids such as OWNER D-60 in this card are Gemma "
        "Grounded Health's decisions, not EvalKit's.</li>"
        f"<li><strong>Models.</strong> Gemma 4 E2B GGUFs (Q4_K_M) served by "
        f"{_code(src['models']['served_by'])}: {roles}. How to run one: "
        f'<a href="{PLUGINS_URL}">docs/plugins.md, “Gemma through llama-server”</a>.</li>'
        f"<li><strong>Judging.</strong> {_e(j['judge'])} alone is the judge, with "
        f"{_e(j['extractor'])} extracting the claims (GGH: “{_code(j['text'])}”). One judge: "
        "self-preference is not measured.</li>"
        f"<li><strong>Calibration.</strong> The run's own EvalKit stamp reads "
        f"“uncalibrated”, and GGH keeps it (“{_e(cal['run_stamp_note'])}”). Both statements "
        "below hold: EvalKit's stamp is per run folder and needs labels scored into that "
        "folder, while GGH's release stamp is computed outside the run against a model "
        f"reference labeller. GGH's release "
        f"stamp for {_e(cal['judge'])}, as its results bundle words it: “{_e(cal['wording'])}”. "
        f"The reference labeller is {_e(cal['reference_labeller'])}: a model's labels, not a "
        "human expert's.</li>"
        f"<li><strong>Aim rule.</strong> {_code(aim['text'])} The citation rubric is "
        f"<code>{_e(aim['citation_rubric_hash'])}</code>; GGH's count is post-hoc "
        f"({_e(aim['decision'])}): “{_e(aim['note'])}” Source: "
        f'<a href="{_e(aim["url"])}"><code>{_e(aim["source"])}</code></a>.</li>'
        f'<li><strong>Weights.</strong> Model: <a href="{_e(links["model"])}">'
        f"{_e(links['model'].removeprefix('https://huggingface.co/'))}</a>; GGUF: "
        f'<a href="{_e(links["gguf"])}">{_e(links["gguf"].removeprefix("https://huggingface.co/"))}'
        "</a>.</li></ul>"
        f"{_aim_tables(aim)}</div>"
    )


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise PageError(f"the rendered report has {text.count(old)} copies of {old[:40]!r}")
    return text.replace(old, new)


def render(gemma: Path = GEMMA) -> str:
    from evalkit.report.html import render_report
    from evalkit.schemas import RunSummary
    from evalkit.schemas.config import Threshold

    summary = RunSummary.model_validate_json((gemma / "summary.json").read_text(encoding="utf-8"))
    run = json.loads((gemma / "run.json").read_text(encoding="utf-8"))
    src = load_source(gemma)
    thresholds = [Threshold.model_validate(t) for t in run.get("thresholds", [])]
    page = render_report(summary, [], {}, thresholds=thresholds)

    title = re.search(r"<title>[^<]*</title>", page)
    if not title:
        raise PageError("the rendered report has no <title>")
    page = page.replace(
        title.group(0), f"<title>{_e(src['project'])} on EvalKit · {_e(summary.run_id)}</title>", 1
    )
    page = _replace_once(page, "<h2 id=headline>", gemma_card(src) + "<h2 id=headline>")
    cal = src["calibration"]
    page = _replace_once(
        page,
        "</div><h2 id=sp-h>",
        f"<p>GGH's release stamp for {_e(cal['judge'])}: “{_e(cal['wording'])}” "
        "(see About this run).</p></div><h2 id=sp-h>",
    )
    start, end = page.find("<h2 id=fail-h>"), page.find("</main>")
    if start < 0 or end < start:
        raise PageError("the rendered report has no failure explorer before </main>")
    note = (
        "<h2 id=fail-h>Per-case records</h2><div class=card><p>Not published here: this page "
        "shows the run's aggregate summary only, so it has no failure explorer.</p></div>"
    )
    return page[:start] + note + page[end:]


def _visible_text(page: str) -> str:
    body = re.sub(r"<(style|script)>.*?</\1>", " ", page, flags=re.S)
    body = re.sub(r"</?(?:code|a|strong|em)\b[^>]*>", "", body)  # inline: no word break
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())


def required_statements(src: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    cal, aim = src["calibration"], src["aim_rule"]
    out = [
        f"{src['judging']['judge']} alone is the judge",
        f"{src['judging']['extractor']} extracting the claims",
        cal["wording"],
        "Claude Opus 5.5 reference labeller",
        "a model's labels, not a human expert's",
        aim["text"].replace("`", ""),
        aim["citation_rubric_hash"],
        summary["run_id"],
        "per-case records are not published here",
    ]
    out += [m["model"] for m in summary["models"]]
    return out


def required_links(src: dict[str, Any]) -> list[str]:
    return [src["aim_rule"]["url"], src["links"]["model"], src["links"]["gguf"]]


def check(gemma: Path = GEMMA) -> list[str]:
    """Every problem with docs/gemma; empty when the page may be published."""
    problems: list[str] = []
    try:
        src = load_source(gemma)
    except (OSError, ValueError) as exc:
        return [f"source.json unreadable: {exc}"]
    for name in COPIED:
        path = gemma / name
        want = src["files"][name]["sha256"]
        if not path.is_file():
            problems.append(f"{name} missing")
        elif sha256(path) != want:
            problems.append(f"{name} differs from GGH's {src['files'][name]['from']} ({want[:12]})")
    for p in sorted(gemma.iterdir()):
        if FORBIDDEN_NAME_RE.search(p.name):
            problems.append(f"{p.name}: per-case files are never copied here")
    page_path = gemma / PAGE
    if not page_path.is_file():
        return [*problems, f"{PAGE} missing (run scripts/build_gemma_page.py)"]
    page = page_path.read_text(encoding="utf-8")
    if not problems:
        try:
            if render(gemma) != page:
                problems.append(f"{PAGE} is stale (run scripts/build_gemma_page.py)")
        except PageError as exc:
            problems.append(str(exc))
    text = _visible_text(page)
    summary = json.loads((gemma / "summary.json").read_text(encoding="utf-8"))
    for s in required_statements(src, summary):
        if " ".join(s.split()) not in text:
            problems.append(f"page does not state: {s[:60]!r}")
    for url in required_links(src):
        if f'href="{_e(url)}"' not in page:
            problems.append(f"page does not link {url}")
    if CASE_ID_RE.search(page):
        problems.append("page shows a case id (pqa-…)")
    long_numbers = LONG_NUMBER_RE.findall(text)
    if long_numbers:
        problems.append(f"page shows an id-like number: {long_numbers[0]}")
    if "<details" in page:
        problems.append("page has per-case failure items")
    return problems


def _ggh_value(obj: Any, path: list[str]) -> Any:
    for key in path:
        obj = obj[key]
    return obj


def cross_check(gemma: Path, ggh: Path) -> list[str]:
    """Compare source.json and the copied files with a GGH checkout."""
    src = load_source(gemma)
    problems: list[str] = []

    def read(rel: str) -> str:
        return (ggh / rel).read_text(encoding="utf-8")

    for name in COPIED:
        rel = src["files"][name]["from"]
        if (ggh / rel).read_bytes() != (gemma / name).read_bytes():
            problems.append(f"{name} differs from GGH {rel}")
    bundle = json.loads(read(src["calibration"]["source"]))
    entry = next(
        (c for c in bundle["calibration"] if c["judge"] == src["calibration"]["judge"]), {}
    )
    for key, field in (
        ("wording", "wording"),
        ("status", "status"),
        ("reference_model", "reference_labeller"),
    ):
        if entry.get(key) != src["calibration"][field]:
            problems.append(f"calibration {field} differs from GGH {src['calibration']['source']}")
    readme = read(src["aim_rule"]["text_source"])
    if src["aim_rule"]["text"] not in readme:
        problems.append("aim-rule text not in GGH's README draft")
    if src["calibration"]["run_stamp_note"] not in readme:
        problems.append("run-stamp note not in GGH's README draft")
    if src["judging"]["text"] not in read(src["judging"]["source"]):
        problems.append("judging text not in GGH's README draft")
    aim = json.loads(read(src["aim_rule"]["source"]))
    for key in ("decision", "post_hoc", "citation_rubric_hash", "note"):
        if aim[key] != src["aim_rule"][key]:
            problems.append(f"aim_rule {key} differs from GGH")
    judge = src["aim_rule"]["judge"]
    for row in src["aim_rule"]["rates"]:
        got = _ggh_value(aim, ["citation", src["run"]["name"], row["model"], judge])
        for count in ("published", "corrected"):
            if got[count] != row[count]:
                problems.append(f"aim_rule {row['model']} {count} differs from GGH")
    fam = aim["family"][src["run"]["name"]]
    for c in src["aim_rule"]["comparisons"]:
        match = [
            g
            for g in fam[c["count"]]
            if (g["metric"], g["model_a"], g["model_b"])
            == (c["metric"], c["model_a"], c["model_b"])
        ]
        if not match or (match[0]["diff"], match[0]["separated"]) != (c["diff"], c["separated"]):
            problems.append(
                f"aim_rule comparison {c['metric']} {c['model_a']} {c['count']} differs"
            )
    import yaml

    cfg = yaml.safe_load(read(src["models"]["source"]))
    if list(cfg["models"]) != list(src["models"]["roles"]):
        problems.append("models differ from GGH's run config")
    if (
        cfg["judges"] != [src["judging"]["judge"]]
        or cfg["extractor"] != src["judging"]["extractor"]
    ):
        problems.append("judge or extractor differs from GGH's run config")
    return problems


def build(gemma: Path = GEMMA) -> Path:
    out = gemma / PAGE
    out.write_text(render(gemma), encoding="utf-8", newline="\n")
    return out


def self_test() -> int:
    """Plant each defect in a copy of docs/gemma and require the check to name it."""
    failures = 0

    def expect(name: str, problems: list[str], needle: str | None) -> None:
        nonlocal failures
        ok = not problems if needle is None else any(needle in p for p in problems)
        failures += 0 if ok else 1
        print(f"  [{'ok' if ok else 'WRONG'}] {name}: {problems[:1] or 'clean'}")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "base"
        shutil.copytree(GEMMA, base)
        build(base)
        expect("built page passes", check(base), None)

        def variant(label: str) -> Path:
            d = Path(tmp) / label
            shutil.copytree(base, d)
            return d

        d = variant("stale")
        page = (d / PAGE).read_text(encoding="utf-8")
        (d / PAGE).write_text(page.replace("</main>", "<p>extra</p></main>"), encoding="utf-8")
        expect("stale page", check(d), "stale")

        d = variant("statement")
        page = (d / PAGE).read_text(encoding="utf-8")
        (d / PAGE).write_text(page.replace("Claude Opus 5.5", "a reviewer"), encoding="utf-8")
        expect("missing calibration statement", check(d), "Claude Opus 5.5")

        d = variant("link")
        page = (d / PAGE).read_text(encoding="utf-8")
        (d / PAGE).write_text(page.replace('aim_rule_check.json"', 'x.json"'), encoding="utf-8")
        expect("missing aim-rule link", check(d), "aim_rule_check.json")

        d = variant("caseid")
        page = (d / PAGE).read_text(encoding="utf-8")
        (d / PAGE).write_text(page.replace("</main>", "<p>pqa-1</p></main>"), encoding="utf-8")
        expect("case id on the page", check(d), "case id")

        d = variant("number")
        page = (d / PAGE).read_text(encoding="utf-8")
        (d / PAGE).write_text(page.replace("</main>", "<p>(12345678)</p></main>"), encoding="utf-8")
        expect("id-like number on the page", check(d), "id-like number")

        d = variant("details")
        page = (d / PAGE).read_text(encoding="utf-8")
        (d / PAGE).write_text(
            page.replace("</main>", "<details><summary>x</summary></details></main>"),
            encoding="utf-8",
        )
        expect("failure item on the page", check(d), "failure items")

        d = variant("edited")
        s = (d / "summary.json").read_text(encoding="utf-8")
        (d / "summary.json").write_text(s.replace("0.404", "0.504", 1), encoding="utf-8")
        expect("edited summary", check(d), "summary.json differs")

        d = variant("records")
        (d / "records.jsonl").write_text("{}\n", encoding="utf-8")
        expect("records file present", check(d), "per-case files")

        d = variant("nopage")
        (d / PAGE).unlink()
        expect("page missing", check(d), "missing")

    print("self-test", "PASSED" if failures == 0 else f"FAILED ({failures})")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--gemma", type=Path, default=GEMMA, help="the docs/gemma folder")
    ap.add_argument("--check", action="store_true", help="check only; write nothing")
    ap.add_argument("--ggh", type=Path, help="a Gemma Grounded Health checkout to compare with")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.check:
        print(f"wrote {build(args.gemma)}")
    problems = check(args.gemma)
    if args.ggh:
        problems += cross_check(args.gemma, args.ggh)
    for p in problems:
        print(f"gemma page: {p}", file=sys.stderr)
    if not problems:
        print("gemma page: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
