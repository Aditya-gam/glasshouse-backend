"""Token counting for the Retriever's budget cap (tiktoken).

A stable BPE encoding gives a model-agnostic soft ceiling on the evidence set (the proxy's resolved
model tokenizes a little differently, but the budget is a soft cap, not an exact bound). The
encoding downloads once on first use, so the retriever takes a `TokenCounter` and tests inject a
fake — `count_tokens` is never invoked in CI.
"""

from collections.abc import Callable
from functools import lru_cache

import tiktoken

TokenCounter = Callable[[str], int]

_ENCODING_NAME = "cl100k_base"


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(_ENCODING_NAME)


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))
