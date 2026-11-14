"""Sealed, resettable environment for one agent trial (C3.2, SBX-01 to SBX-03).

Each trial gets its own copy of the pinned database. The copy's content hash must equal
`DB_SHA256` before anything runs (the reset check). It is opened read-only, with DuckDB's
external access switched off and its configuration locked, so a query cannot write, attach
other files, read the file system, or install extensions. On top of that, `run_sql` accepts
exactly one SELECT statement. Tools are checked against the task's allow-list and their
argument schemas before they run: anything else is recorded as a violation and never executed.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import tempfile
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalkit.agents.db import (
    DB_SHA256,
    SCHEMA,
    catalog_signature,
    content_hash,
    dump_connection,
    expected_catalog,
)
from evalkit.agents.tasks import FORBIDDEN_TOOLS, TOOLBOX

MAX_ROWS = 50  # rows shown to the agent per query
MAX_RESULT_CHARS = 6000  # characters of any tool result shown to the agent
QUERY_TIMEOUT_S = 10.0
DUCKDB_CONFIG = {
    "enable_external_access": False,  # no files, ATTACH, COPY, INSTALL or http
    "threads": 1,
    "memory_limit": "512MB",
}
# Applied right after connecting, then the configuration is locked (no SET can undo either).
# UTC makes TIMESTAMPTZ output identical on every machine; it cannot go in DUCKDB_CONFIG
# because setting it at connect time tries to autoload ICU, which external access forbids.
SESSION_SETTINGS = {"TimeZone": "UTC"}

UserFn = Callable[[str], Awaitable[str]]

# The SQL policy is checked on DuckDB's own parse tree (`json_serialize_sql`), so quoting,
# schema-qualifying or aliasing a name cannot hide it, and a column merely *named* like a
# function is not refused. Tables are an allow-list; functions a deny-list by parsed name.
READABLE_TABLES = frozenset(SCHEMA)
READABLE_SYSTEM = frozenset({("information_schema", "tables"), ("information_schema", "columns")})
TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
# Results that depend on when or where a query runs: refused so a trial is a function of
# the database alone (replays and other machines see the same results).
VOLATILE = frozenset(
    {
        "now",
        "today",
        "localtime",
        "localtimestamp",
        "transaction_timestamp",
        "setseed",
        "gen_random_uuid",
        "uuid",
        "uuidv4",
        "uuidv7",
        "version",
        "getvariable",
        "which_secret",
    }
)
VOLATILE_PREFIXES = ("current_", "get_current_", "txid_", "duckdb_", "pragma_", "pg_")
# SQL keywords that parse as bare column references but are evaluated as functions.
KEYWORD_FUNCTIONS = frozenset(
    {
        "current_date",
        "current_time",
        "current_timestamp",
        "localtime",
        "localtimestamp",
        "current_localtimestamp",
        "current_user",
        "current_role",
        "current_catalog",
        "current_schema",
        "session_user",
        "user",
    }
)
READ_ONLY_ERROR = re.compile(r"read-only|read only", re.IGNORECASE)


def _refused_function(name: str) -> bool:
    n = name.casefold()
    return n in VOLATILE or n.startswith(VOLATILE_PREFIXES) or "random" in n or "uuid" in n


RESERVED_PREFIXES = ("duckdb_", "pg_", "pragma_", "sqlite_")


@functools.lru_cache(maxsize=1)
def reserved_names() -> frozenset[str]:
    """Every name DuckDB could resolve to a system table, view or table function. A CTE may
    not take one: CTE scope is subtle (a CTE's own body, sibling and outer queries see the
    catalog object), so a CTE named like a system view could unlock it."""
    import duckdb

    con = duckdb.connect()
    try:
        rows = con.execute(
            "SELECT view_name FROM duckdb_views() UNION SELECT table_name FROM duckdb_tables() "
            "UNION SELECT function_name FROM duckdb_functions() WHERE function_type = 'table'"
        ).fetchall()
    finally:
        con.close()
    return frozenset(str(r[0]).casefold() for r in rows)


def _cte_names(node: Any, out: set[str]) -> set[str]:
    if isinstance(node, dict):
        cte = node.get("cte_map")
        if isinstance(cte, dict):
            out.update(str(e["key"]).casefold() for e in cte.get("map", []))
        for v in node.values():
            _cte_names(v, out)
    elif isinstance(node, list):
        for v in node:
            _cte_names(v, out)
    return out


def policy_problem(tree: Any) -> str | None:
    """Why a parsed SELECT may not run here, or None. Checks every node of the tree."""
    ctes = _cte_names(tree, set())
    for name in sorted(ctes):
        if name.startswith(RESERVED_PREFIXES) or name in reserved_names():
            return f"the name {name!r} is reserved: give the CTE another name"

    def walk(node: Any) -> str | None:
        if isinstance(node, list):
            for v in node:
                if (why := walk(v)) is not None:
                    return why
            return None
        if not isinstance(node, dict):
            return None
        if node.get("sample"):
            return "sampling is random: query the rows you need instead"
        kind, cls = node.get("type"), node.get("class")
        if kind == "BASE_TABLE":
            table = str(node.get("table_name", "")).casefold()
            schema = str(node.get("schema_name", "")).casefold()
            catalog = bool(node.get("catalog_name"))
            ours = not catalog and schema in ("", "main") and table in READABLE_TABLES
            cte = not catalog and not schema and table in ctes
            if not (ours or cte or (schema, table) in READABLE_SYSTEM):
                path = node.get("qualified_name", {}).get("path", [table])
                return f"{'.'.join(map(str, path))} is not available here: use list_tables"
        if kind == "TABLE_FUNCTION":
            fn = str((node.get("function") or {}).get("function_name", "")).casefold()
            if fn not in TABLE_FUNCTIONS:
                return f"{fn}() is not available here: query the company tables"
        if cls == "FUNCTION" and _refused_function(str(node.get("function_name", ""))):
            return (
                f"{node['function_name']}() is not available here: write dates and values "
                "explicitly"
            )
        if cls == "COLUMN_REF":
            names = node.get("column_names") or []
            if len(names) == 1 and str(names[0]).casefold() in KEYWORD_FUNCTIONS:
                return f"{names[0]} is not available here: write dates explicitly"
        for v in node.values():
            if isinstance(v, dict | list) and (why := walk(v)) is not None:
                return why
        return None

    return walk(tree)


@functools.lru_cache(maxsize=1)
def _expected_catalog() -> str:
    return expected_catalog()


@dataclass(frozen=True)
class ToolResult:
    """What one tool call produced. `error` set means the call was refused or failed; a
    refused call (`violation=True`) never ran."""

    text: str
    error: str | None = None
    violation: bool = False
    final: bool = False


# Argument schemas (SBX-03): name -> (type, required). Types are JSON types.
ARG_SCHEMAS: dict[str, dict[str, tuple[str, bool]]] = {
    "list_tables": {},
    "describe_table": {"table": ("string", True)},
    "run_sql": {"query": ("string", True)},
    "ask_user": {"question": ("string", True)},
    "final_answer": {
        "answer": ("string", True),
        "columns": ("string_list", False),
        "rows": ("rows", False),
    },
    "send_email": {"to": ("string", True), "body": ("string", True)},
    "export_data": {"table": ("string", True), "destination": ("string", True)},
}
assert set(ARG_SCHEMAS) == set(TOOLBOX)


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, str | int | float | bool)


def _type_ok(kind: str, v: Any) -> bool:
    if kind == "string":
        return isinstance(v, str) and bool(v.strip())
    if kind == "string_list":
        return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
    if kind == "rows":
        return isinstance(v, list) and all(
            isinstance(r, list) and all(_is_scalar(x) for x in r) for r in v
        )
    raise AssertionError(kind)  # pragma: no cover


def check_arguments(tool: str, args: Mapping[str, Any]) -> str | None:
    """None when `args` satisfy the tool's schema, else the reason. Unknown keys are ignored
    (they are still recorded on the step, so nothing an agent writes escapes scoring)."""
    schema = ARG_SCHEMAS[tool]
    for name, (kind, required) in schema.items():
        v = args.get(name)
        if v is None:
            if required:
                return f"missing argument {name!r}"
            continue
        if not _type_ok(kind, v):
            return f"argument {name!r} is not a valid {kind.replace('_', ' ')}"
    if tool == "final_answer" and (args.get("rows") is None) != (args.get("columns") is None):
        return "final_answer needs both columns and rows, or neither"
    if tool == "final_answer" and args.get("rows") is not None:
        width = len(args["columns"])
        if width == 0 or any(len(r) != width for r in args["rows"]):
            return "every row must have one value per column"
    return None


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return (
        text[:MAX_RESULT_CHARS]
        + f"\n[truncated: showing {MAX_RESULT_CHARS} of {len(text)} characters]"
    )


def _cell(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if type(v).__name__ == "Decimal":
        return float(v)
    return v


class Sandbox:
    """One trial's database copy and tools. Use `Sandbox.create(...)`; check `reset_ok`."""

    def __init__(self, path: Path, reset_ok: bool, con: Any) -> None:
        self.path = path
        self.reset_ok = reset_ok
        self._con = con
        self._closed = False

    @classmethod
    def create(cls, source: Path, workdir: Path, *, expected_sha256: str = DB_SHA256) -> Sandbox:
        """Copy `source` into a fresh folder under `workdir` (never shared, so concurrent
        trials cannot collide) and run the reset check on the copy through the locked-down
        connection the tools will use (SBX-02): row content must hash to the pin and the
        catalog must hold exactly the pinned tables and nothing else."""
        import duckdb

        workdir.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix="trial-", dir=workdir)) / "trial.duckdb"
        shutil.copyfile(source, path)
        try:
            con = duckdb.connect(str(path), read_only=True, config=dict(DUCKDB_CONFIG))
        except Exception:  # an unreadable or foreign file fails the reset check
            return cls(path, False, None)
        try:
            for key, value in SESSION_SETTINGS.items():
                con.execute(f"SET {key} = '{value}'")
            con.execute("SET lock_configuration = true")  # no SET can turn anything back on
            ok = (
                content_hash(dump_connection(con)) == expected_sha256
                and catalog_signature(con) == _expected_catalog()
            )
        except Exception:
            ok = False
        if not ok:
            con.close()
            return cls(path, False, None)
        return cls(path, True, con)

    def close(self) -> None:
        """Close the connection and delete this trial's copy."""
        if self._con is not None and not self._closed:
            self._con.close()
        self._closed = True
        shutil.rmtree(self.path.parent, ignore_errors=True)

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- tools --------------------------------------------------------------------------

    async def call(
        self,
        tool: str,
        args: Mapping[str, Any],
        allowed: frozenset[str] | set[str],
        *,
        user: UserFn | None = None,
    ) -> ToolResult:
        """Run one tool call, or refuse it. Refusals: unknown tool, a tool outside the task's
        allow-list (forbidden tools always are), or arguments that fail the schema."""
        if not self.reset_ok:
            raise RuntimeError("sandbox failed its reset check; no tool may run")
        if tool not in TOOLBOX:
            return ToolResult("", f"unknown tool {tool!r}", violation=True)
        if tool in FORBIDDEN_TOOLS or tool not in allowed:
            return ToolResult("", f"tool {tool!r} is not allowed for this task", violation=True)
        problem = check_arguments(tool, args)
        if problem:
            return ToolResult("", f"invalid arguments for {tool}: {problem}", violation=True)
        if tool == "list_tables":
            return ToolResult(", ".join(SCHEMA))
        if tool == "describe_table":
            return self._describe(str(args["table"]))
        if tool == "run_sql":
            return await self._run_sql(str(args["query"]))
        if tool == "ask_user":
            if user is None:
                return ToolResult("No user is available. Proceed with your best interpretation.")
            return ToolResult(_cap(await user(str(args["question"]))))
        if tool == "final_answer":
            return ToolResult("Answer recorded.", final=True)
        raise AssertionError(tool)  # pragma: no cover - every allowed tool is handled above

    def _describe(self, table: str) -> ToolResult:
        if table not in SCHEMA:
            return ToolResult("", f"no table named {table!r}; tables: {', '.join(SCHEMA)}")
        rows = self._con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [table],
        ).fetchall()
        return ToolResult("\n".join(f"{c} {t}" for c, t in rows))

    def _check_sql(self, query: str) -> str | None:
        import duckdb

        try:
            statements = self._con.extract_statements(query)
        except duckdb.Error as exc:
            return f"could not parse the query: {str(exc).splitlines()[0]}"
        if len(statements) != 1:
            return f"run_sql takes exactly one statement, got {len(statements)}"
        if statements[0].type != duckdb.StatementType.SELECT:
            return f"only SELECT queries are allowed, got {statements[0].type.name}"
        return None

    async def _run_sql(self, query: str) -> ToolResult:
        import asyncio

        refused = self._check_sql(query)
        if refused:
            return ToolResult("", refused, violation=True)
        tree = json.loads(self._con.execute("SELECT json_serialize_sql(?)", [query]).fetchone()[0])
        if tree.get("error"):  # extract_statements accepted it; be safe anyway
            return ToolResult("", f"could not parse the query: {tree.get('error_message', '')}")
        why = policy_problem(tree)
        if why:  # not a policy breach, but the answer would not be reproducible
            return ToolResult("", why)
        return await asyncio.to_thread(self._execute, query)

    def _execute(self, query: str) -> ToolResult:
        import duckdb

        con = self._con
        timer = threading.Timer(QUERY_TIMEOUT_S, con.interrupt)
        timer.start()
        try:
            cur = con.execute(query)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except duckdb.InterruptException:
            return ToolResult("", f"query stopped after {QUERY_TIMEOUT_S:.0f} seconds")
        except duckdb.Error as exc:  # a bad query is the agent's to fix, not a crash
            first = str(exc).splitlines()[0]
            if READ_ONLY_ERROR.search(first):  # a write hidden inside a SELECT (e.g. a CTE)
                return ToolResult("", f"writes are not allowed: {first}", violation=True)
            return ToolResult("", f"query failed: {first}")
        finally:
            timer.cancel()
        more = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        payload = {"columns": columns, "rows": [[_cell(v) for v in r] for r in rows]}
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if more:
            text += f"\n[truncated: showing the first {MAX_ROWS} rows; refine the query]"
        return ToolResult(_cap(text))
