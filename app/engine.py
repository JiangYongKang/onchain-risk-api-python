"""Pure, deterministic evaluation engine.

The same functions back live risk queries, historical replay and dry-runs,
which is what guarantees "dry-run output == post-publish output" for the same
(data version, rule set) pair. Nothing here touches global state.
"""
from __future__ import annotations

from app.models import DiffEntry, EvalSide, Rule, RuleHit, Transaction, TxRiskResult


class GraphFeatures:
    """Derived address-graph features for one snapshot."""

    def __init__(self, fan_out: dict[str, int], fan_in: dict[str, int],
                 cyclic_addresses: frozenset[str]) -> None:
        self.fan_out = fan_out
        self.fan_in = fan_in
        self.cyclic_addresses = cyclic_addresses


def _strongly_connected_components(adj: dict[str, set[str]]) -> list[set[str]]:
    """Tarjan's SCC, iterative to stay safe on deep graphs."""
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[set[str]] = []
    counter = [0]

    for root in adj:
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_idx = work[-1]
            if child_idx == 0:
                index_of[node] = lowlink[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)
            recurse = False
            children = sorted(adj.get(node, ()))
            for i in range(child_idx, len(children)):
                nxt = children[i]
                if nxt not in index_of:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    recurse = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if recurse:
                continue
            if work[-1][0] != node:
                pass
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                scc: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.add(member)
                    if member == node:
                        break
                sccs.append(scc)
    return sccs


def build_graph_features(transactions: tuple[Transaction, ...]) -> GraphFeatures:
    """Compute fan-out / fan-in / cycle membership for one snapshot."""
    adj: dict[str, set[str]] = {}
    fan_in: dict[str, int] = {}
    self_loops: set[str] = set()
    for tx in transactions:
        adj.setdefault(tx.from_addr, set()).add(tx.to_addr)
        adj.setdefault(tx.to_addr, set())
        fan_in[tx.to_addr] = fan_in.get(tx.to_addr, 0) + 1
        fan_in.setdefault(tx.from_addr, fan_in.get(tx.from_addr, 0))
        if tx.from_addr == tx.to_addr:
            self_loops.add(tx.from_addr)
    fan_out = {addr: len(targets) for addr, targets in adj.items()}

    cyclic: set[str] = set(self_loops)
    for scc in _strongly_connected_components(adj):
        if len(scc) > 1:
            cyclic |= scc
    return GraphFeatures(fan_out=fan_out, fan_in=fan_in,
                         cyclic_addresses=frozenset(cyclic))


def _rule_matches(tx: Transaction, features: GraphFeatures, rule: Rule) -> bool:
    c = rule.condition
    if c.min_amount is not None and tx.amount < c.min_amount:
        return False
    if c.max_amount is not None and tx.amount > c.max_amount:
        return False
    if c.assets is not None and tx.asset not in c.assets:
        return False
    if c.from_addrs is not None and tx.from_addr not in c.from_addrs:
        return False
    if c.to_addrs is not None and tx.to_addr not in c.to_addrs:
        return False
    if c.min_fan_out is not None:
        if features.fan_out.get(tx.from_addr, 0) < c.min_fan_out:
            return False
    if c.min_fan_in is not None:
        if features.fan_in.get(tx.to_addr, 0) < c.min_fan_in:
            return False
    if c.in_cycle is not None:
        in_cycle = (tx.from_addr in features.cyclic_addresses
                    or tx.to_addr in features.cyclic_addresses)
        if in_cycle != c.in_cycle:
            return False
    return True


def evaluate_transaction(tx: Transaction, features: GraphFeatures,
                         rules: tuple[Rule, ...]) -> TxRiskResult:
    """Evaluate one transaction against an ordered rule set.

    Deterministic: hits follow rule order; score is the sum of hit scores;
    labels are de-duplicated preserving first-seen order.
    """
    hits: list[RuleHit] = []
    labels: list[str] = []
    score = 0
    for rule in rules:
        if _rule_matches(tx, features, rule):
            hits.append(RuleHit(rule_id=rule.rule_id, score=rule.score,
                                labels=list(rule.labels)))
            score += rule.score
            for label in rule.labels:
                if label not in labels:
                    labels.append(label)
    return TxRiskResult(tx_id=tx.tx_id, score=score, labels=labels, hits=hits)


def evaluate_snapshot(features: GraphFeatures,
                      transactions: tuple[Transaction, ...],
                      rules: tuple[Rule, ...]) -> dict[str, TxRiskResult]:
    return {tx.tx_id: evaluate_transaction(tx, features, rules)
            for tx in transactions}


def _side(result: TxRiskResult | None) -> EvalSide:
    if result is None or not result.hits:
        return EvalSide(score=0, labels=[], hits=[])
    return EvalSide(score=result.score, labels=list(result.labels),
                    hits=list(result.hits))


def diff_results(candidate: dict[str, TxRiskResult],
                 baseline: dict[str, TxRiskResult]) -> list[DiffEntry]:
    """Per-transaction diff between two evaluation results.

    - added:   hit only under the candidate rules
    - removed: hit only under the baseline rules
    - changed: hit under both, but score or labels differ
    Each entry carries the contributing rule ids on both sides.
    """
    diffs: list[DiffEntry] = []
    for tx_id in sorted(set(candidate) | set(baseline)):
        cand = candidate.get(tx_id)
        base = baseline.get(tx_id)
        cand_hit = cand is not None and bool(cand.hits)
        base_hit = base is not None and bool(base.hits)
        if cand_hit and not base_hit:
            change_type = "added"
        elif base_hit and not cand_hit:
            change_type = "removed"
        elif cand_hit and base_hit and (cand.score != base.score
                                        or cand.labels != base.labels):
            change_type = "changed"
        else:
            continue
        diffs.append(DiffEntry(tx_id=tx_id, change_type=change_type,
                               candidate=_side(cand), baseline=_side(base)))
    return diffs
