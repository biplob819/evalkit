"""The portfolio walkthrough GIF (docs/assets/walkthrough.gif): its capture script compiles and
the committed GIF matches its shot list and the size limit (owner decision relayed by the
portfolio sessions: 6-10 screens of committed outputs, under about 3 MB)."""

from __future__ import annotations

import py_compile
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_capture_script_compiles():
    py_compile.compile(str(ROOT / "scripts" / "capture_walkthrough.py"), doraise=True)


def test_the_gif_is_small_and_its_shot_list_names_six_to_ten_screens():
    gif = ROOT / "docs" / "assets" / "walkthrough.gif"
    data = gif.read_bytes()
    assert data[:6] in (b"GIF87a", b"GIF89a")
    assert len(data) < 3_000_000
    shots = re.findall(r"^\| \d+ \|", (ROOT / "docs" / "walkthrough.md").read_text("utf-8"), re.M)
    assert 6 <= len(shots) <= 10


def test_the_portfolio_metadata_points_at_the_gif():
    text = (ROOT / "docs" / "portfolio.yaml").read_text("utf-8")
    assert "docs/assets/walkthrough.gif" in text
