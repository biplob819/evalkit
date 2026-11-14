# B2.3 acceptance criteria

**REL-01** `readme_chart(summary)` returns a standalone SVG whose marks match the summary's headline points and CIs, and whose text contains every `stamp_lines` caveat; identical summaries give identical SVG.
**REL-02** The README opens with the chart, three generated sentences that quote the summary's numbers with intervals, a three-line install-and-run snippet, and the PubMedQA credit; a test regenerates the block from the committed summary and finds no drift.
**REL-03** The run report is published as a static page by a reproducible script or workflow, and the README links to it.
