# S2.2 acceptance criteria

**RET-01** `recall_at_k` counts distinct relevant ids in the first k returned positions, divided by the number of relevant ids; a duplicate occupies a slot but earns nothing; k < 1 or no relevant ids raises.
**RET-02** `mrr` is 1/position of the first relevant id (1-based, positions as returned), 0.0 when none is retrieved.
**RET-03** `ndcg_at_k` with binary or graded gains matches hand-computed values on fixtures, is 1.0 for the ideal ordering and 0.0 with no relevant ids retrieved; negative gains raise.
