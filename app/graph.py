"""Address-relation graph features on an immutable data snapshot.

The graph combines explicit address relations (edges) with transaction flows
(``from -> to``). Transactions are walked in a deterministic order
(``(ts, tx_id)``) so that:

* ``loop``: a transaction closes a directed cycle when, before adding its edge,
  its destination can already reach its source.
* ``fanout``: the number of distinct outgoing neighbours of the source after
  including this transaction.

Because ordering and tie-breaks are fixed, two evaluations over the same data
version always produce identical features.
"""

from __future__ import annotations

from collections import defaultdict

from app.models import Edge, Transaction


class GraphSnapshot:
    def __init__(
        self,
        transactions: list[Transaction] | tuple[Transaction, ...],
        edges: list[Edge] | tuple[Edge, ...] = (),
    ) -> None:
        self._adj: dict[str, set[str]] = defaultdict(set)
        for e in edges:
            if e.frm != e.to:
                self._adj[e.frm].add(e.to)

        ordered = sorted(transactions, key=lambda t: (t.ts, t.tx_id))
        self._loop: set[str] = set()
        self._fanout: dict[str, int] = {}
        for tx in ordered:
            if tx.frm != tx.to and self._reaches(tx.to, tx.frm):
                self._loop.add(tx.tx_id)
            self._adj[tx.frm].add(tx.to)
            self._fanout[tx.tx_id] = len(self._adj[tx.frm])

    def _reaches(self, src: str, dst: str) -> bool:
        """Whether ``src`` can reach ``dst`` over current directed edges."""
        stack = [src]
        seen: set[str] = {src}
        while stack:
            cur = stack.pop()
            if cur == dst:
                return True
            for nxt in self._adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return False

    def is_loop(self, tx_id: str) -> bool:
        return tx_id in self._loop

    def fanout(self, tx_id: str) -> int:
        return self._fanout.get(tx_id, 0)
