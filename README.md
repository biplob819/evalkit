# EvalKit

<!-- evalkit:headline:start -->
<p align="center"><img src="https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/headline.svg" alt="On 200 PubMedQA questions, 4 models (claude-sonnet-5-5, claude-haiku-5-5, gpt-6.1-sol, gpt-6-luna) answered from the abstract and cited its sentences; two judges from different families (claude-opus-5-5 and gpt-6.1-sol) checked every claim. The most grounded model was gpt-6.1-sol, correct and fully grounded on 57.5% (95% CI 50.5%–64.0%) by claude-opus-5-5, 46.0% (95% CI 39.0%–53.0%) by gpt-6.1-sol; it is separated from every other model after a Holm correction (gpt-6.1-sol also judged its own answers; claude-opus-5-5 agrees); the most accurate model was claude-sonnet-5-5 (77.0% correct labels); the headline also requires every claim to be supported by the sentences the answer cites. Caveats: judge not calibrated for this task (see docs/calibrate-your-task.md). Treat these numbers as provisional." width="760"></p>

On 200 PubMedQA questions, 4 models (claude-sonnet-5-5, claude-haiku-5-5, gpt-6.1-sol, gpt-6-luna) answered from the abstract and cited its sentences; two judges from different families (claude-opus-5-5 and gpt-6.1-sol) checked every claim. The most grounded model was gpt-6.1-sol, correct and fully grounded on 57.5% (95% CI 50.5%–64.0%) by claude-opus-5-5, 46.0% (95% CI 39.0%–53.0%) by gpt-6.1-sol; it is separated from every other model after a Holm correction (gpt-6.1-sol also judged its own answers; claude-opus-5-5 agrees); the most accurate model was claude-sonnet-5-5 (77.0% correct labels); the headline also requires every claim to be supported by the sentences the answer cites. Caveats: judge not calibrated for this task (see docs/calibrate-your-task.md). Treat these numbers as provisional.

[Full report](https://biplob819.github.io/evalkit/) · results in [`docs/results/`](https://github.com/biplob819/evalkit/tree/main/docs/results)

```bash
pip install 'evalkit-harness[anthropic,openai]'
evalkit init   # scaffold a project and a three-case sample task
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run   # estimate
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3   # needs keys in .env
```

Questions from [PubMedQA](https://pubmedqa.github.io/) (Jin et al., 2019, MIT licence); see [DATA_CARD.md](https://github.com/biplob819/evalkit/blob/main/DATA_CARD.md). Research benchmark, not medical advice.
<!-- evalkit:headline:end -->

Evaluate **your own task** across Claude and OpenAI models: accuracy, groundedness, citation validity, cost and latency side by side, with confidence intervals and two cross-family judges whose error rate is measured against held-out hand labels.

> **Status: v0.1.0, the first public release** ([on PyPI](https://pypi.org/project/evalkit-harness/)). To work on EvalKit itself, clone it and run `uv sync --all-extras`.

**Try it in the browser:** the [live demo](https://evalkit-demo.streamlit.app/) shows four models answering the same question from a study abstract, with every claim checked against the sentences it cites. Replay mode is open to everyone; live mode needs a passcode from the owner and then runs on the owner's keys or your own.

**Start here:** [Quickstart](https://github.com/biplob819/evalkit/blob/main/docs/quickstart.md) (a first result in under five minutes) · [Write your own task](https://github.com/biplob819/evalkit/blob/main/docs/writing-a-task.md) (JSONL or CSV) · [Control cost](https://github.com/biplob819/evalkit/blob/main/docs/cost.md) · [Calibrate the judges](https://github.com/biplob819/evalkit/blob/main/docs/calibrate-your-task.md) · [Scorers and adapters](https://github.com/biplob819/evalkit/blob/main/docs/plugins.md) · [Run it in CI](https://github.com/biplob819/evalkit/blob/main/examples/github-action/evalkit.yml) · [Versioning](https://github.com/biplob819/evalkit/blob/main/docs/versioning.md)

Built-in tasks: `pubmedqa` (yes / no / maybe research questions) and `scifact` (supports / refutes / not enough info for scientific claims); see [DATA_CARD.md](https://github.com/biplob819/evalkit/blob/main/DATA_CARD.md).

No telemetry: data goes only to the providers you configure ([PRIVACY.md](https://github.com/biplob819/evalkit/blob/main/PRIVACY.md)). Security and key handling: [SECURITY.md](https://github.com/biplob819/evalkit/blob/main/SECURITY.md). Contributions welcome: [CONTRIBUTING.md](https://github.com/biplob819/evalkit/blob/main/CONTRIBUTING.md).

The package is published as `evalkit-harness` (the name `evalkit` on PyPI belongs to an unrelated project). The command and the import are both `evalkit`.

- Product: [EvalKit PRD](https://github.com/biplob819/evalkit/blob/main/EvalKit%20PRD.md)
- Design: [EvalKit Tech Spec](https://github.com/biplob819/evalkit/blob/main/EvalKit%20Tech%20Spec.md)
- Build status: `uv run python scripts/status.py`

MIT licence.
