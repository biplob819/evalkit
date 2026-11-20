# DRAFT, not filed: citation rubric `be34ebe577bc96ae` — does the aim rule apply to a claim that only restates the aim?

*Drafted in B5.17 from Gemma Grounded Health's evidence (GGH OWNER D-62, D-64). Not filed; the owner decides whether and where to file it. Every number below is from GGH's `docs/measurements/aim_rule_check.json` at its commit 22579b3 ([on GitHub](https://github.com/biplob819/gemma-grounded-health/blob/22579b3/docs/measurements/aim_rule_check.json)).*

---

**Title:** Citation rubric: say whether an aim sentence supports a claim that only restates the aim

**Component:** citation-support judge (B5.12), `evalkit/pipeline/judge.py`, `CITATION_RUBRIC`; rubric hash `CITATION_PROMPT_HASH = be34ebe577bc96ae` (0.4.0, 0.4.1).

### The rule as written

> - "supports" is true only if this sentence by itself states or directly implies the claim, or the part of the claim it is cited for. Use only that sentence: no outside knowledge and no other sentence. A sentence that only asks a question or states an aim or a hypothesis does not establish it.

The last sentence is meant to stop an answer citing "We aimed to test whether X reduces Y" as evidence that X reduces Y. It does not say what happens when the claim itself is about the aim or design ("The study evaluated whether X reduces Y", "This was a randomized trial of X"). For such a claim the aim sentence states the claim exactly, so the first sentence of the rule says it supports, and the last sentence says it does not.

### What the judge does

GPT-6.1 Sol applies the last sentence even to claims that only restate the study's aim or design: it marks the aim-role sentence as not supporting, while the claim judge (same model, `JUDGE_PROMPT_HASH 9a3a8246049f6f85`) marks the same claim supported and lists that sentence as its evidence. The two judges then disagree about the same (claim, sentence) pair, and the citation figures count a correct citation as a miss and as an aim citation. GGH found this in a sample read of its v2 main run (`r-715fc213a8a5`, 1,000 PubMedQA cases, Sol alone as judge, GPT-6 Luna extracting) and counted it after the results were read (post-hoc).

### Evidence

GGH's post-hoc count ("corrected") treats an aim-role cited sentence as supporting when all of these hold: the citation judge gave it role `aim`; the claim judge's verdict on the claim is `supported`; the sentence is in that verdict's evidence; the claim text matches GGH's aim-or-design pattern (abridged: `aim|objective|purpose|…|this study|randomized|prospective|cohort|trial`; the full pattern is in the JSON). Everything else is unchanged.

Run `main_v2`, judge gpt-6.1-sol, published (pre-registered) → corrected:

| Model | Citation precision | Aim-citation rate |
|---|---|---|
| Gemma 4 E2B base (`gemma-e2b-base-q4`) | 0.747 → 0.783 | 0.138 → 0.054 |
| v1 fine-tune (`gemma-e2b-grounded-q4`) | 0.362 → 0.365 | 0.560 → 0.554 |
| v2 fine-tune (`gemma-e2b-grounded-v2-q4`) | 0.736 → 0.737 | 0.084 → 0.083 |

The base model restates aims often, so its figures move most; v1 and v2 barely move. Paired differences (95% intervals, Holm-separated across GGH's family of 6):

| Comparison | Published | Corrected |
|---|---|---|
| v2 − base, citation precision | −0.011 (−0.046 to +0.023), tie | −0.047 (−0.081 to −0.012), separated |
| v2 − base, aim-citation rate | −0.054 (−0.080 to −0.028), separated (v2 better) | +0.030 (+0.008 to +0.051), separated (base better) |
| v2 − v1, citation precision | +0.374 (+0.344 to +0.403), separated | +0.372 (+0.341 to +0.401), separated |
| v2 − v1, aim-citation rate | −0.476 (−0.508 to −0.443), separated | −0.471 (−0.503 to −0.438), separated |

So the v2-vs-base citation result reverses under the other reading, and v2-vs-v1 holds under both. GGH kept its pre-registered numbers and discloses that v2-vs-base citation results depend on this reading (GGH OWNER D-62). The same check on GGH's v1 run (`main_citation`) moves base's aim-citation rate 0.142 → 0.053 under Sol and 0.057 → 0.043 under Claude Opus 5.5, whose figures move far less.

This is a rubric ambiguity, not a code bug: the judge follows one defensible reading of the text.

### Proposed clarification (proposal only)

Add one sentence after "A sentence that only asks a question or states an aim or a hypothesis does not establish it.":

> That applies to claims about findings. When the claim itself only states the study's question, aim, hypothesis or design, a sentence that states that aim or design does support it.

Optionally, add a per-sentence flag (`aim_restated: true`) so the aim-citation rate can be reported with and without such pairs.

Any change to `CITATION_RUBRIC` makes a new `CITATION_PROMPT_HASH`, so citation verdicts cached under `be34ebe577bc96ae` are not reused and earlier runs are not comparable without re-judging. Runs judged under the current hash keep their numbers; a release note should say which hash a figure was judged under. The claim judge (`JUDGE_PROMPT_HASH`) is unaffected.

### Questions for maintainers

1. Is the intended reading the one Sol applies (an aim sentence never supports, whatever the claim), or the one above?
2. If the rubric changes, should the old hash stay selectable for reproducing published runs?
