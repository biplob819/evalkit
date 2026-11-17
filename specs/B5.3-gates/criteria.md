# B5.3 acceptance criteria

**GAT-01** Gates match any named metric by name and labels (`*` for every value), with `>=`, `<=`, `==` on the point value or the 95% CI lower or upper bound; a gate matching nothing, or a bound without an interval, exits 2; v0.2 `RunConfig.thresholds` keep their meaning and bytes.
**GAT-02** `evalkit gate <summary> [--config] [--json] [--out]` prints every gate and the verdict, exits 0/1/2 (3/4 for EvalKit run summaries stopped at the cap or pending), makes no model or network call, and the GitHub Action example uses it.
**GAT-03** The `metrics-1` format is validated exactly as written in spec.md (required fields, unit domains, unique name+labels, intervals, refusal of unknown top-level keys); `evalkit gate` reads it and EvalKit's `summary.json` alike.
**GAT-04** Held-out splits appear only as aggregates; no case id can be present or printed; long free-text label values are not echoed; assay-pipeline's one-success case fails both gates with exit 1.
**GAT-05** Multi-field labels, any unit including NTD, supplied intervals carried exactly, and the `simulated` flag shown in the output and `gate.json`; respectful-offers' exported fixture passes its gates.
**GAT-06** A `disparity` gate computes the max−min group gap per combination of other labels with a seeded 95% interval and gates it; respectful-offers' age-band case is a test.
**GAT-07** `evalkit.stats` exposes an exact Clopper-Pearson interval (`1 - alpha**(1/n)` upper bound at k = 0, beta quantiles otherwise), checked against independent values (n=150: k=0 → 0.0198, k=1 → 0.0365); reports say "0 of n, below X% at 95%", never "0%".
**GAT-08** Each gate has `severity` `fail` (default) or `warn`; warn failures exit 0 but are printed and present in the JSON.
**GAT-09** Exit precedence 2 > 3 > 4 > 1 > 0; the input-classes table exists in the docs with a test that fails on any unlisted input; numeric domains are checked without float conversion; `gate.json` is written on every exit path.
**GAT-10** Latency percentiles and per-case cost gate like any other metric (units ms/s and currency).
