# B2.1 acceptance criteria

**DSH-01** `write_report` produces `report.html` with no external URLs in `src`/`href` and no network requests, plus a valid `charts.vl.json`; identical inputs give identical bytes.
**DSH-02** The scatter places each model and judge at (cost per 100, headline) with whiskers at the CI bounds (checked numerically from the SVG), shows the threshold line when a headline threshold exists, and has a data table with the same numbers.
**DSH-03** The model table shows every ModelSummary figure and a PASS/FAIL badge with text, the failed thresholds verbatim, and the pairwise table shows separated/tie with "unadjusted" on the intervals.
**DSH-04** The failure explorer lists exactly the answers that are not correct-and-grounded for each judge, with claims, both verdicts and cited sentences, and escapes every piece of model text (an injected `<script>` stays inert).
**DSH-05** A calibrated run shows kappa, confusion and self-consistency; an uncalibrated one shows the stamp and guide link instead; the self-preference table appears only in dual mode.
**DSH-06** The research banner and every `stamp_lines` caveat appear at the top; a pending run shows "judging pending" and no headline figures; every chart has a title and description, badges do not rely on colour, and interactive controls are focusable buttons or links.
