"""Capture docs/assets/walkthrough.gif from committed outputs only (no keys, no model call, $0).

    uv run --extra anthropic --extra openai --with markdown \
        python scripts/capture_walkthrough.py --scratch /path/to/scratch

Every frame is a committed file or the real output of an offline EvalKit command on a committed
config or fixture. Terminal output is shown as a monospace page. Command outputs go to the
git-ignored runs/walkthrough/ folder and are removed afterwards. Case ids in the failure explorer
are masked. The frames are screenshotted at 1280x800 with headless Chrome and joined with ffmpeg
(2.5 s a frame, 960 px wide, 128 colours, looping). See docs/walkthrough.md for the shot list.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import markdown  # pyright: ignore[reportMissingModuleSource]  # run with `uv run --with markdown`

ROOT = Path(__file__).resolve().parents[1]
OUT_RUNS = "runs/walkthrough"  # git-ignored
W, H = 1280, 800

CSS = """
:root{--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--soft:#f6f8fa}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 40px}
h1{font-size:28px;border-bottom:1px solid var(--line);padding-bottom:6px;margin:0 0 14px}
h2{font-size:21px;border-bottom:1px solid var(--line);padding-bottom:4px;margin:20px 0 10px}
p{margin:0 0 12px}
code{font:13px ui-monospace,Menlo,monospace;background:var(--soft);padding:1px 4px;
 border-radius:4px}
pre{background:var(--soft);padding:12px;border-radius:6px;overflow:hidden}
pre code{background:none;padding:0}
table{border-collapse:collapse;margin:0 0 14px;font-size:13.5px}
th,td{border:1px solid var(--line);padding:5px 9px;text-align:left;vertical-align:top}
th{background:var(--soft)}
a{color:#0969da;text-decoration:none}
.tag{display:inline-block;font:12px ui-monospace,Menlo,monospace;color:var(--muted);
 border:1px solid var(--line);border-radius:12px;padding:1px 9px;margin:0 0 12px}
.pass{color:#1a7f37;font-weight:600}.drift{color:#9a6700;font-weight:600}
"""
TERM_CSS = """
body{margin:0;background:#0d1117;color:#e6edf3}
.win{margin:0;height:800px;box-sizing:border-box;padding:0}
.bar{height:34px;background:#21262d;display:flex;align-items:center;padding:0 14px;gap:8px;
 font:13px -apple-system,Helvetica,sans-serif;color:#8b949e}
.dot{width:12px;height:12px;border-radius:50%;display:inline-block}
pre{margin:0;padding:16px 22px;font-family:ui-monospace,Menlo,monospace;white-space:pre;
 overflow:hidden}
.p{color:#7ee787}.c{color:#e6edf3;font-weight:600}.ok{color:#7ee787}.dim{color:#8b949e}
"""


def page(title: str, body: str, css: str = CSS) -> str:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}'
        f"</title><style>{css}</style></head><body>{body}</body></html>"
    )


def md_page(title: str, text: str, tag: str) -> str:
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    return page(title, f'<main><span class="tag">{html.escape(tag)}</span>{body}</main>')


def run(argv: list[str]) -> str:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["COLUMNS"] = "120"
    p = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    if p.returncode not in (0, 1):
        sys.exit(f"{' '.join(argv)} exited {p.returncode}:\n{p.stdout}\n{p.stderr}")
    return p.stdout


def terminal(title: str, shown_cmd: str, output: str, max_lines: int = 33) -> str:
    lines = output.rstrip("\n").splitlines()
    if len(lines) > max_lines:  # keep the head and the last line (the verdict), mark the cut
        cut = len(lines) - max_lines + 1
        lines = [*lines[: max_lines - 2], f"  … ({cut} more lines)", lines[-1]]
    width = max(len(shown_cmd) + 12, *(len(x) for x in lines))
    size = min(15.0, (W - 44) / (width * 0.602))
    out = []
    for x in lines:
        e = html.escape(x)
        if x.startswith(("estimated total", "verdict:")):
            e = f'<span class="ok">{e}</span>'
        out.append(e)
    prompt = f'<span class="p">~/evalkit $</span> <span class="c">{html.escape(shown_cmd)}</span>'
    dots = "".join(
        f'<span class="dot" style="background:{c}"></span>'
        for c in ("#ff5f57", "#febc2e", "#28c840")
    )
    body = (
        f'<div class="win"><div class="bar">{dots}<span style="margin-left:10px">'
        f"{html.escape(title)}</span></div>"
        f'<pre style="font-size:{size:.2f}px;line-height:1.38">{prompt}\n' + "\n".join(out)
    )
    return page(title, body + "</pre></div>", TERM_CSS)


def readme_headline() -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    head, rest = text.split("<!-- evalkit:headline:start -->", 1)
    block = rest.split("<!-- evalkit:headline:end -->", 1)[0]
    intro = "\n".join(
        x for x in head.splitlines() if not x.startswith("[![")
    )  # badges are live images; left out so nothing is fetched
    raw = "https://raw.githubusercontent.com/biplob819/evalkit/main/"
    src = (intro + block).replace(raw, (ROOT.as_uri() + "/"))
    return md_page("EvalKit README", src, "README.md · headline block")


REPORT_JS = """<script>
(function(){
 var w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),n;
 while((n=w.nextNode())){if(/pqa-\\d+/.test(n.nodeValue))
  n.nodeValue=n.nodeValue.replace(/pqa-\\d+/g,'[case id masked]');}
 var d=document.querySelector('#failures details');if(d)d.open=true;  /* first failure open */
 var t=document.getElementById('%s');
 if(t){var e=t.previousElementSibling;  /* show the page from this section down */
  while(e){var p=e.previousElementSibling;e.style.display='none';e=p;}
  t.style.marginTop='8px';}
})();
</script>"""


def report_view(target: str) -> str:
    text = (ROOT / "docs" / "results" / "report.html").read_text(encoding="utf-8")
    js = REPORT_JS % target
    i = text.rfind("</body>")
    return text[:i] + js + text[i:] if i >= 0 else text + js


HISTORY_JS = """<script>
(function(){  /* lead with the leakage chart (the alarm); the Latest table is left out */
 var l=document.getElementById('latest-h'),t=document.getElementById('trend-h');
 if(l){l.nextElementSibling.style.display='none';l.style.display='none';}
 var cards=[].slice.call(document.querySelectorAll('h2#trend-h ~ .card'));
 var k=cards.filter(function(c){var x=c.querySelector('svg title');
  return x&&/leakage/i.test(x.textContent);})[0];
 if(t&&k)t.parentNode.insertBefore(k,t.nextSibling);
})();
</script>"""


def history_view(text: str) -> str:
    i = text.rfind("</body>")
    return text[:i] + HISTORY_JS + text[i:] if i >= 0 else text + HISTORY_JS


def canary_page() -> str:
    rel = "docs/measurements/downstream-canary-2026-11-18.json"
    d = json.loads((ROOT / rel).read_text(encoding="utf-8"))
    rows = []
    for name, p in sorted(d["projects"].items()):
        c, a = p["counts"], p["audit"]
        cls = "pass" if p["status"] == "pass" else "drift"
        rows.append(
            f"<tr><td>{html.escape(name)}</td><td class={cls}>{html.escape(p['status'])}</td>"
            f"<td>{c['passed']}</td><td>{c['failed']}</td><td>{c['skipped']}</td>"
            f"<td>{len(p['files'])}</td><td>{a['public']}</td><td>{a['public-elsewhere']}</td>"
            f"<td>{a['internal']}</td><td>{a['private']}</td></tr>"
        )
    head = (
        "<tr><th>downstream project</th><th>status</th><th>passed</th><th>failed</th>"
        "<th>skipped</th><th>test files</th><th>imports: public</th><th>public elsewhere</th>"
        "<th>internal</th><th>private</th></tr>"
    )
    body = (
        f'<main><span class="tag">{rel}</span><h1>Downstream canary, {d["date"]}</h1>'
        f"<p>Each downstream project's own EvalKit tests, run against EvalKit "
        f"{html.escape(d['evalkit_version'])} source, with every EvalKit import it makes audited "
        f"against the public API. Spend: ${d['spend_usd']}. <code>known-drift</code> marks a "
        f"failure already traced to the project's pinned version, not to EvalKit.</p>"
        f"<table>{head}{''.join(rows)}</table></main>"
    )
    return page("Downstream canary", body)


def security_page() -> str:
    rel = "tests/fixtures/security/golden/report.md"
    text = (ROOT / rel).read_text(encoding="utf-8")
    text = text.split("## Public cases", 1)[0]  # the per-case table names case ids
    return md_page("Security eval", text, f"{rel} · golden output of the security kit")


def decisions_page() -> str:
    rel = "docs/decisions.md"
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    keep = lines[:15]  # the header and D-1 to D-9: what fits one screen
    return md_page("Decision log", "\n".join(keep), f"{rel} · append-only decision log")


def chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac).exists():
        return mac
    sys.exit("headless Chrome not found (google-chrome, chromium or Google Chrome.app)")


def shoot(browser: str, html_file: Path, png: Path, profile: Path) -> None:
    subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--allow-file-access-from-files",
            f"--user-data-dir={profile}",
            f"--window-size={W},{H}",
            "--virtual-time-budget=4000",
            "--run-all-compositor-stages-before-draw",
            f"--screenshot={png}",
            html_file.as_uri(),
        ],
        check=True,
        capture_output=True,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "Capture the walkthrough GIF.").splitlines()[0]
    )
    ap.add_argument("--scratch", required=True, type=Path, help="frames folder (not committed)")
    ap.add_argument("--gif", type=Path, default=ROOT / "docs" / "assets" / "walkthrough.gif")
    ap.add_argument("--no-gif", action="store_true", help="screenshots only")
    args = ap.parse_args()
    scratch: Path = args.scratch.resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    for old in [*scratch.glob("seq*.png"), *scratch.glob("frame*.html")]:
        old.unlink()
    (ROOT / OUT_RUNS).mkdir(parents=True, exist_ok=True)
    ek = [str(Path(sys.executable).parent / "evalkit")]  # the venv's console script

    dry_cmd = "evalkit run configs/pubmedqa.yaml --task pubmedqa --n 200 --dry-run"
    gate_cmd = (
        "evalkit gate tests/fixtures/gates/assay/metrics.json "
        "--config tests/fixtures/gates/assay/gates.yaml"
    )
    dry = run([*ek, *dry_cmd.split()[1:]])
    gate = run([*ek, *gate_cmd.split()[1:], "--out", f"{OUT_RUNS}/gate.json"])
    gate = gate.replace(f"gate.json: {OUT_RUNS}/gate.json\n", "")
    run(
        [
            *ek,
            "compare",
            "tests/fixtures/compare/assay/old/metrics.json",
            "tests/fixtures/compare/assay/new/metrics.json",
            "--gates",
            "tests/fixtures/gates/assay/gates.yaml",
            "--out",
            f"{OUT_RUNS}/compare.json",
            "--md",
            f"{OUT_RUNS}/compare.md",
        ]
    )
    compare_md = (ROOT / OUT_RUNS / "compare.md").read_text(encoding="utf-8")
    compare_md = compare_md.replace("settings.\n- ", "settings.\n\n- ")  # a markdown list
    # The ledger fixture of tests/test_ledger_trend.py: eight fraud-review-bedrock runs with a
    # settings change, a band reset and a leakage alarm (its leakage chart is the committed
    # golden tests/fixtures/ledger/trend-leakage.svg).
    sys.path.insert(0, str(ROOT))
    from tests.test_ledger_trend import _fraud_ledger

    ledger = _fraud_ledger(ROOT / OUT_RUNS).relative_to(ROOT)
    run(
        [
            *ek,
            "history",
            "show",
            "--ledger",
            str(ledger),
            "--html",
            f"{OUT_RUNS}/history.html",
        ]
    )
    history = (ROOT / OUT_RUNS / "history.html").read_text(encoding="utf-8")
    shutil.rmtree(ROOT / OUT_RUNS)

    frames = [
        ("README headline", readme_headline()),
        ("report: headline and scatter", report_view("")),
        ("report: failure explorer", report_view("fail-h")),
        ("dry run", terminal("evalkit run --dry-run: cost quote", dry_cmd, dry)),
        ("gate", terminal("evalkit gate on a metrics-1 fixture", gate_cmd, gate)),
        (
            "compare",
            md_page("Run comparison", compare_md, "evalkit compare … --md: fixture runs"),
        ),
        ("history", history_view(history)),
        ("security", security_page()),
        ("canary", canary_page()),
        ("decisions", decisions_page()),
    ]
    browser = chrome()
    profile = scratch / "chrome-profile"
    for i, (_, doc) in enumerate(frames, 1):
        f = scratch / f"frame{i:02d}.html"
        f.write_text(doc, encoding="utf-8")
        shoot(browser, f, scratch / f"seq{i:02d}.png", profile)
        print(f"seq{i:02d}.png  {frames[i - 1][0]}")
    if args.no_gif:
        return
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found")
    args.gif.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            "1/2.5",
            "-i",
            str(scratch / "seq%02d.png"),
            "-vf",
            "fps=10,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=5",
            "-loop",
            "0",
            str(args.gif),
        ],
        check=True,
    )
    print(f"{args.gif}  {args.gif.stat().st_size} bytes, {len(frames)} frames")


if __name__ == "__main__":
    main()
