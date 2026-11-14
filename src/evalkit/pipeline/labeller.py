"""Local labelling page (OWNER D-40, CAL-02).

A stdlib HTTP server bound to 127.0.0.1. It shows one claim at a time with its cited
sentences and nothing else: no judge verdicts, no model names, no private claim ids. Keys:
S supported, U unsupported, E toggles "extraction error", Backspace goes back. Every label is
written to the labels file atomically before the page moves on, so closing the tab loses
nothing. In the relabel round only relabel answers are shown, in a fresh order, and the
first-pass labels are never sent to the page.
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from evalkit.pipeline.calibrate import LabelEntry, LabelFile, SheetRow

Round = Literal["first", "relabel"]


def load_labels(path: Path) -> LabelFile | None:
    if not path.is_file():
        return None
    return LabelFile.model_validate_json(path.read_text(encoding="utf-8"))


def save_labels(path: Path, labels: LabelFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".labels-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(labels.model_dump_json(indent=1))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Labeller:
    """State behind the page: the ordered items for this round and the labels file."""

    def __init__(self, rows: Sequence[SheetRow], labels_path: Path, round_: Round, seed: int):
        items = [r for r in rows if round_ == "first" or r.relabel]
        if round_ == "relabel":
            # A fresh order, grouped by answer, so position does not cue the first-pass label.
            answers = sorted({r.answer for r in items})
            random.Random(f"evalkit-relabel:{seed}").shuffle(answers)
            rank = {a: i for i, a in enumerate(answers)}
            items.sort(key=lambda r: (rank[r.answer], r.key))
        if not rows:
            raise ValueError("empty sheet")
        self.sheet_id = rows[0].sheet_id
        self.round: Round = round_
        existing = load_labels(labels_path)
        if existing is not None and (existing.sheet_id, existing.round) != (self.sheet_id, round_):
            raise ValueError(
                f"{labels_path} holds {existing.round}-round labels for sheet {existing.sheet_id}; "
                f"use a separate file for the {round_} round of sheet {self.sheet_id}"
            )
        self.items = items
        self.keys = {r.key for r in items}
        self.path = labels_path
        self.labels: dict[str, LabelEntry] = dict(existing.labels) if existing else {}
        self.lock = threading.Lock()
        self.port = 0  # set once the server is bound

    def view(self, index: int | None = None) -> dict[str, Any]:
        """The next unlabelled item (or `index`), with only the blind fields."""
        with self.lock:
            done = sum(1 for r in self.items if r.key in self.labels)
            if index is None:
                index = next(
                    (i for i, r in enumerate(self.items) if r.key not in self.labels),
                    len(self.items),
                )
            if index >= len(self.items):
                return {"done": True, "total": len(self.items), "labelled": done}
            r = self.items[max(0, index)]
            current = self.labels.get(r.key)
            return {
                "done": False,
                "index": max(0, index),
                "total": len(self.items),
                "labelled": done,
                "key": r.key,
                "claim": r.claim_text,
                "cited": [{"n": n, "text": t} for n, t in r.cited],
                "label": current.label if current else None,
                "extraction_error": current.extraction_error if current else False,
            }

    def record(self, key: str, label: str, extraction_error: bool) -> None:
        with self.lock:
            if key not in self.keys:
                raise KeyError(key)
            entry = LabelEntry(label=label, extraction_error=extraction_error)  # type: ignore[arg-type]
            self.labels[key] = entry
            save_labels(
                self.path, LabelFile(sheet_id=self.sheet_id, round=self.round, labels=self.labels)
            )


PAGE = (resources.files("evalkit.pipeline") / "labeller.html").read_text(encoding="utf-8")


MAX_BODY = 10_000


def make_handler(state: Labeller) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _host_ok(self) -> bool:
            """Refuse other Host headers, so a web page cannot reach the labeller through
            DNS rebinding."""
            port = state.port
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if self.path == "/":
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path.startswith("/api/item"):
                index = None
                if "?index=" in self.path:
                    try:
                        index = int(self.path.split("?index=", 1)[1])
                    except ValueError:
                        return self._json(400, {"error": "bad index"})
                self._json(200, state.view(index))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "bad host"})
            if self.path != "/api/label":
                return self._json(404, {"error": "not found"})
            # application/json forces a CORS preflight, so other sites cannot post labels.
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "JSON only"})
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._json(411, {"error": "length required"})
            if not 0 < size <= MAX_BODY:
                return self._json(413, {"error": "body too large or empty"})
            try:
                body = json.loads(self.rfile.read(size))
                key, label, err = body["key"], body["label"], body.get("extraction_error", False)
                if not isinstance(key, str) or not isinstance(label, str):
                    raise TypeError("key and label must be strings")
                if not isinstance(err, bool):
                    raise TypeError("extraction_error must be true or false")
                state.record(key, label, err)
            except (KeyError, ValueError, TypeError) as exc:
                return self._json(400, {"error": str(exc)})
            self._json(200, {"ok": True})

    return Handler


def serve_labeller(
    rows: Sequence[SheetRow],
    labels_path: Path,
    *,
    round_: Round = "first",
    seed: int = 0,
    port: int = 0,
) -> ThreadingHTTPServer:
    """Start the page on 127.0.0.1 (never another interface). Returns the server; the caller
    runs `serve_forever()` (or a thread) and reads `server.server_address` for the port."""
    state = Labeller(rows, labels_path, round_, seed)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    state.port = int(server.server_address[1])
    server.labeller = state  # type: ignore[attr-defined]
    return server
