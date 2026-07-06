"""Lightweight entity extraction for shared-entity association edges (Idea #4).

Pure, stdlib-only, heuristic-first: pull the concrete named things out of a fact's text
(file paths, dotted qualnames, snake_case / CamelCase identifiers) so two facts that mention
the same entity can be linked. Deliberately conservative — over-extraction produces false
edges, so common English words and bare short tokens are not entities. An optional
distiller-backed extractor can layer on top later behind the same call site; this is the
zero-dependency floor.
"""

from __future__ import annotations

import re

# file-ish paths (core/store.py, dataset.json), dotted qualnames (core.recall.search),
# snake_case (add_records), and CamelCase (RetentionWeights).
_PATTERNS = (
    re.compile(r"\b[\w-]+(?:/[\w.-]+)+\b"),  # a/b/c paths
    re.compile(r"\b\w+\.\w{1,5}\b"),  # file.ext
    re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b"),  # snake_case
    re.compile(r"\b[a-z0-9]+\.[a-z0-9]+(?:\.[a-z0-9]+)+\b", re.I),  # dotted.qual.name
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b"),  # CamelCase
)


def extract_entities(text: str) -> set[str]:
    """Return the set of entity tokens in ``text`` (lower-cased). Pure and deterministic.

    Conservative: only the structured patterns above count, so ordinary prose contributes
    nothing (no false edges from shared common words).
    """
    if not text:
        return set()
    found: set[str] = set()
    for pattern in _PATTERNS:
        for match in pattern.findall(text):
            token = match.strip(".").lower()
            if len(token) >= 4:  # drop noise like "a.b"
                found.add(token)
    return found
