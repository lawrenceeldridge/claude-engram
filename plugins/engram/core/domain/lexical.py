"""Lexical primitives (Functional Core — pure).

Shared tokenisation for the lexical channel of rank fusion. Deliberately tiny and dependency-free: lower-case
alphanumeric tokens, common stop-words dropped, single/double-char noise removed.
"""

from __future__ import annotations

import re

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
