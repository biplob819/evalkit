# EvalKit

Evaluate **your own task** across Claude and OpenAI models: accuracy, groundedness, citation validity, cost and latency side by side, with confidence intervals and two cross-family judges whose error rate is measured against held-out hand labels.

> **Status: pre-alpha (v0.0.1).** This release reserves the package name and contains only the project skeleton. The first usable release is v0.1.0.

```bash
pip install evalkit-harness
evalkit --version
```

The package is published as `evalkit-harness` (the name `evalkit` on PyPI belongs to an unrelated project). The command and the import are both `evalkit`.

- Product: [EvalKit PRD](EvalKit%20PRD.md)
- Design: [EvalKit Tech Spec](EvalKit%20Tech%20Spec.md)
- Build status: `uv run python scripts/status.py`

Research benchmark, not medical advice. MIT licence.

The flagship task uses [PubMedQA](https://pubmedqa.github.io/) (Jin et al., 2019, MIT licence); see [DATA_CARD.md](DATA_CARD.md).
