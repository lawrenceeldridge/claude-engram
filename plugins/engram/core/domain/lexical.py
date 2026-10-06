"""Lexical primitives (Functional Core — pure).

Shared tokenisation for the lexical channel of rank fusion. Deliberately tiny and dependency-free: lower-case
alphanumeric tokens, common stop-words dropped, single/double-char noise removed.
"""

from __future__ import annotations

import bisect
import itertools
import re
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9]+")

_STOP = frozenset(
    "the a an of to in on at for and or is are was were be been being it its this that "
    "with as by from into out up down over under how what when where why who which do "
    "does did has have had can could should would will i you we they he she them our your".split()
)


def tokenize(text: str) -> list[str]:
    """Content tokens: lower-case alphanumerics, stop-words and <3-char noise removed."""
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 2 and t not in _STOP]


def token_set(text: str) -> set[str]:
    return set(tokenize(text))


_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_SEP = "\x00"


def overlap_counts(query_tokens: set[str], texts: Sequence[str]) -> list[int]:
    """``len(query_tokens & token_set(text))`` for every text — without tokenising every text.

    A content token (what ``token_set`` keeps) is in ``token_set(text)`` exactly when the
    lower-cased text holds it as a maximal ``[a-z0-9]`` run; any other query token can never
    match, so it is dropped up front. One ``str.find`` sweep per token over a single joined, lower-cased blob, with a
    boundary check at each hit, finds those runs; a hit's row comes from the cumulative
    offsets. If lower-casing changed a length or a text holds the separator, offsets would
    drift, so that rare case takes the per-text definition instead.
    """
    counts = [0] * len(texts)
    query_tokens = query_tokens & token_set(" ".join(query_tokens))
    if not query_tokens or not texts:
        return counts
    blob = _SEP.join(texts).lower()
    starts = list(itertools.accumulate((len(text) + 1 for text in texts), initial=0))
    if len(blob) != starts[-1] - 1 or blob.count(_SEP) != len(texts) - 1:
        return [len(query_tokens & token_set(text)) for text in texts]
    end = len(blob)
    found: dict[int, set[str]] = {}
    for token in query_tokens:
        size = len(token)
        at = blob.find(token)
        while at != -1:
            before = blob[at - 1] if at else _SEP
            after = blob[at + size] if at + size < end else _SEP
            if before not in _ALNUM and after not in _ALNUM:
                found.setdefault(bisect.bisect_right(starts, at) - 1, set()).add(token)
            at = blob.find(token, at + size)
    for row, tokens in found.items():
        counts[row] = len(tokens)
    return counts
