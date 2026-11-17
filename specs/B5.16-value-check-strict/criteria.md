# B5.16 acceptance criteria

**SEV-01 (value check, strict)** [D-72, D-74]
1. No false claim is `verified`: every reproduction from verifier passes 1–7 (verify513, b–g probes) is unverifiable or contradicted; gen.py (verify513f) and gen2.py (verify513g, seeds 7 and 11, full size) report 0 verified false claims.
2. A number is bound to a field only by a whole field name or explicit alias (case/space/underscore-insensitive) or a `phrases` match, in the documented shapes; never by closeness of value. Unknown subjects, partial or plural names, typos, coordinated subjects, "respectively" lists, several candidate fields, times without a declared hour field, constants, undeclared percent/unit scaling and approximate words are unverifiable.
3. `contradicted` only when a bound statement clearly fails; negated, hedged or denied statements never contradict.
4. Integer fields are exact ("card history is 44" on 40 is contradicted, never verified); non-integer fields need displayed precision, within max(0.005, 0.5%) and at most 10% off (a coarser correct rounding is unverifiable), or an explicit relative `tolerance`.
5. Percent needs a declared unit (`units={"risk_score": "fraction"}`); otherwise a "%" number is unverifiable.
6. Clear true bound claims are verified (positive tests).
7. D-72 output shape: outcome, per-check status, value 1.0 only when verified, `value_check_scorer` per-reason counts and shares, `value_check_shares` unchanged.
8. Never raises on record contents (verify513c/d/g fuzzers raise nothing); linear in reason length.
9. fraud-review-bedrock's recorded reasons measured (verified / contradicted / unverifiable) and 30 verified reasons hand-checked true.
10. Gates: ruff check, ruff format --check, pyright (all extras), pytest.
11. fraud-review-bedrock's D-42 mutation test (its tests/test_valuecheck.py, adapted in scratch against `value_check`) verifies 0 false claims; where its rules are stricter they are adopted.
12. Closed world (verify516b): every numeric token (digits anywhere, NFKC digits, number words) is consumed by exactly one framed binding or the reason is unverifiable (only `ignore` spans and field names exempt); a binding counts only when framed by a clause start (article or own-word possessive only) and a clause end or an "and"/","-joined framed statement. gen4 (seeds 1, 2) verifies no false claim and contradicts no hedge; gen, gen2 (7, 11), gen3 (1, 2) and the D-42 mutation test verify no false claim; simple true statements ("Amount is $812.40.", "Hour is 3.") still verify.
