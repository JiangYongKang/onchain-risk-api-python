"""Bounded address-relation graph traversal.

Guarantees
----------
* Deterministic order: neighbours are visited in sorted-address order.
* Hard termination under cycles, huge fan-out and huge connected
  components via explicit caps. Caps never cause silent truncation: when a
  cap binds, the result carries ``truncated=True`` plus a stable reason,
  and the boundary counterparties are flagged.
* Back edges (including edges back to the queried address) are detected
  and reported in ``cycles`` / ``cycle=True`` rather than causing infinite
  recursion or an error.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .models import RelatedCounterparty


@dataclass
class GraphResult:
    counterparties: list[RelatedCounterparty]
    truncated: bool
    truncation_reason: str | None = None
    cycles: list[str] = field(default_factory=list)


def traverse(relations_out: dict[str, frozenset[str]],
             relations_in: dict[str, frozenset[str]],
             relation_kind: dict[tuple[str, str], str],
             start: str,
             *,
             max_fanout: int,
             max_nodes: int,
             max_depth: int) -> GraphResult:
    """Breadth-first walk over the *undirected* relation graph from ``start``."""
    results: list[RelatedCounterparty] = []
    by_addr: dict[str, RelatedCounterparty] = {}
    cycles: set[str] = set()
    visited: set[str] = {start}
    parents: dict[str, str] = {start: start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    truncated = False
    reasons: set[str] = set()

    def neighbours(addr: str):
        outs = relations_out.get(addr, frozenset())
        ins = relations_in.get(addr, frozenset())
        joined = sorted(set(outs) | set(ins))
        return joined[:max_fanout], len(joined) > max_fanout

    while queue:
        addr, depth = queue.popleft()
        if depth >= max_depth:
            continue
        nbrs, fanout_hit = neighbours(addr)
        if fanout_hit:
            truncated = True
            reasons.add("FANOUT_TOO_LARGE")
        stop = False
        for nb in nbrs:
            kind = relation_kind.get((addr, nb)) or relation_kind.get((nb, addr))
            if nb in visited:
                if parents.get(addr) != nb:
                    # edge to an already-visited node that is not our BFS
                    # parent => back edge (cycle). Record both endpoints;
                    # start never becomes a counterparty row.
                    cycles.add(nb)
                    cycles.add(addr)
                    if nb != start and nb in by_addr:
                        by_addr[nb].cycle = True
                continue
            visited.add(nb)
            parents[nb] = addr
            row = RelatedCounterparty(address=nb, distance=depth + 1, relation=kind)
            results.append(row)
            by_addr[nb] = row
            if len(visited) - 1 >= max_nodes:
                truncated = True
                reasons.add("GRAPH_NODE_CAP")
                stop = True
                break
            queue.append((nb, depth + 1))
        if stop:
            break

    if truncated:
        reason = "+".join(sorted(reasons))
        for r in results:
            r.truncated = True
            r.truncation_reason = reason
    results.sort(key=lambda r: (r.distance, r.address))
    return GraphResult(results, truncated,
                       "+".join(sorted(reasons)) if reasons else None,
                       sorted(cycles))
