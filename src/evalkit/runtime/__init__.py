"""The EvalKit runtime: typed nodes, content-addressed cache, async executor, JSONL writer."""

# Internal names EvalKit's own modules import from here (re-exported with `as`, so type
# checkers accept those imports); they are not in `__all__` and not public.
from evalkit.runtime.canonical import canonical_json as canonical_json
from evalkit.runtime.canonical import sha256_hex as sha256_hex
from evalkit.runtime.executor import BudgetExceeded, Executor
from evalkit.runtime.node import Node as Node
from evalkit.runtime.node import RunContext as RunContext
from evalkit.runtime.store import Lineage as Lineage
from evalkit.runtime.store import MemoryStore as MemoryStore
from evalkit.runtime.store import Store
from evalkit.runtime.writer import JsonlWriter as JsonlWriter

# Public since 0.4.0 (D-61 rule 2: ai-visibility and fraud-review-bedrock run
# `faithfulness(executor=...)` live): Executor, Store and BudgetExceeded, which Executor raises
# at its cost cap. The other names here stay internal.
__all__ = ["BudgetExceeded", "Executor", "Store"]
