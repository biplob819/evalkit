"""Judge and extractor identities (since 0.3.0, B5.2): the values every public `Record`
carries as `judge_prompt_hash` and `extractor_version`. A cached or recorded result made under
other values was judged or extracted differently."""

from evalkit.pipeline.extract import EXTRACTOR_VERSION
from evalkit.pipeline.judge import JUDGE_PROMPT_HASH

__all__ = ["EXTRACTOR_VERSION", "JUDGE_PROMPT_HASH"]
