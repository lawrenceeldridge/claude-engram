"""Spreading activation over the fact association graph (ACT-R: Lovett, Reder & Lebiere).

Pure Functional Core: the shell loads a bounded set of edges among the current recall
candidates; this module only does arithmetic. A candidate is boosted when it is linked to
*other candidates* — co-activation within the retrieved set — so a fact related to what the
query already surfaced rises, without any graph I/O on the ranking path.
"""

from __future__ import annotations

Edge = tuple[str, str, float]  # (src_id, dst_id, weight) — undirected


def spread(candidate_ids: list[str], edges: list[Edge], weight: float) -> dict[str, float]:
    """Return a boost per candidate: ``weight * (sum of edge weights to other candidates)``.

    Only edges whose *both* endpoints are in ``candidate_ids`` contribute, so activation
    spreads within the retrieved set (not out to the whole store). Pure and deterministic;
    ``weight <= 0`` yields no boosts (the disabled path).
    """
    if weight <= 0 or not candidate_ids or not edges:
        return {}
    present = set(candidate_ids)
    boosts: dict[str, float] = {}
    for src, dst, w in edges:
        if src in present and dst in present and src != dst:
            boosts[src] = boosts.get(src, 0.0) + weight * w
            boosts[dst] = boosts.get(dst, 0.0) + weight * w
    return boosts
