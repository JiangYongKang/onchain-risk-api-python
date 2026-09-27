"""Risk rule evaluation.

Every rule that fires yields its own :class:`RuleEvidence` with the exact
rule id, per-rule version and rule-set version it came from — a single risk
query never mixes rules from different rule sets. Rule ordering is fixed
(rule_id, version) so results are deterministic.

Rule kinds
----------
TX_AMOUNT       tx amount >= params.min_amount
TIME_WINDOW     > params.max_count transactions by the same sender inside
                the trailing window params.window_ms (the evaluated tx is
                the window's right edge; ties broken by tx_hash)
ADDRESS_FANOUT  sender (or receiver) interacted with >= params.min_count
                distinct counterparties within params.within_ms
WATCHED_ADDRESS from/to matches a configured watchlist address
"""
from __future__ import annotations

from .models import RuleEvidence
from .store import RuleDef, StoredTx


def evaluate(tx: StoredTx, ruleset, snapshot, graph_result) -> list[RuleEvidence]:
    evidence: list[RuleEvidence] = []
    rules = sorted(ruleset.rules, key=lambda r: (r.rule_id, r.version))
    for rule in rules:
        hit = _dispatch(rule, tx, snapshot, graph_result)
        if hit is not None:
            evidence.append(RuleEvidence(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                rule_set_version=rule.rule_set_version,
                kind=rule.kind,
                tag=rule.tag,
                score=rule.score,
                detail=hit,
            ))
    return evidence


def _dispatch(rule: RuleDef, tx: StoredTx, snapshot, graph_result) -> str | None:
    if rule.kind == "TX_AMOUNT":
        return _tx_amount(rule, tx)
    if rule.kind == "TIME_WINDOW":
        return _time_window(rule, tx, snapshot)
    if rule.kind == "ADDRESS_FANOUT":
        return _address_fanout(rule, tx, snapshot)
    if rule.kind == "WATCHED_ADDRESS":
        return _watched(rule, tx)
    return None


def _tx_amount(rule: RuleDef, tx: StoredTx) -> str | None:
    minimum = rule.params["min_amount"]
    if tx.amount >= minimum:
        return f"amount {tx.amount:g} >= threshold {minimum:g}"
    return None


def _time_window(rule: RuleDef, tx: StoredTx, snapshot) -> str | None:
    window_ms = rule.params["window_ms"]
    max_count = rule.params["max_count"]
    lo = tx.ts_ms - window_ms
    same_sender = [
        t for t in snapshot.transactions.values()
        if t.from_addr == tx.from_addr and lo <= t.ts_ms <= tx.ts_ms
    ]
    # deterministic tie-breaking; the evaluated tx is always included
    same_sender.sort(key=lambda t: (t.ts_ms, t.tx_hash))
    count = len(same_sender)
    if count > max_count:
        hashes = ",".join(t.tx_hash for t in same_sender)
        return (f"{count} transactions from {tx.from_addr} within {window_ms}ms "
                f"(limit {max_count}); chain=[{hashes}]")
    return None


def _address_fanout(rule: RuleDef, tx: StoredTx, snapshot) -> str | None:
    direction = rule.params["direction"]
    min_count = rule.params["min_count"]
    within_ms = rule.params["within_ms"]
    anchor = tx.from_addr if direction == "out" else tx.to_addr
    lo = tx.ts_ms - within_ms
    counterparties: set[str] = set()
    for t in snapshot.transactions.values():
        if t.ts_ms < lo or t.ts_ms > tx.ts_ms:
            continue
        if direction == "out" and t.from_addr == anchor:
            counterparties.add(t.to_addr)
        elif direction == "in" and t.to_addr == anchor:
            counterparties.add(t.from_addr)
    if len(counterparties) >= min_count:
        ordered = ",".join(sorted(counterparties))
        return (f"{anchor} has {len(counterparties)} distinct {direction} "
                f"counterparties within {within_ms}ms (threshold {min_count}); "
                f"set=[{ordered}]")
    return None


def _watched(rule: RuleDef, tx: StoredTx) -> str | None:
    addresses = set(rule.params["addresses"])
    match = rule.params["match"]
    hit_from = match in ("from", "either") and tx.from_addr in addresses
    hit_to = match in ("to", "either") and tx.to_addr in addresses
    if hit_from or hit_to:
        side = "from_addr" if hit_from else "to_addr"
        return f"{side} {tx.from_addr if hit_from else tx.to_addr} is on watchlist"
    return None
