"""Deterministic rule evaluation.

Given an immutable data snapshot (transactions + :class:`GraphSnapshot`) and a
fixed, ordered tuple of rules, evaluation is a pure function: same input always
yields the same per-transaction hits/scores/tags. Hits are sorted by rule id
and tags are de-duplicated/sorted, which is what lets a dry-run result equal a
later published result byte-for-byte.
"""

from __future__ import annotations

from app.errors import ValidationError
from app.graph import GraphSnapshot
from app.models import Rule, RuleHit, RuleType, Transaction, TxResult


def validate_rules(rules: tuple[Rule, ...] | list[Rule]) -> None:
    seen: set[str] = set()
    for r in rules:
        if r.rule_id in seen:
            raise ValidationError(
                "duplicate rule_id", details={"rule_id": r.rule_id}
            )
        seen.add(r.rule_id)
        p = r.params
        if r.rule_type is RuleType.AMOUNT_OVER:
            _num(p, "threshold", r.rule_id)
        elif r.rule_type is RuleType.ADDRESS_BLACKLIST:
            addrs = p.get("addresses")
            if not isinstance(addrs, list) or not all(
                isinstance(a, str) and a for a in addrs
            ) or not addrs:
                raise ValidationError(
                    "address_blacklist requires non-empty addresses list",
                    details={"rule_id": r.rule_id},
                )
        elif r.rule_type is RuleType.HIGH_FANOUT:
            v = _num(p, "threshold", r.rule_id)
            if v < 1:
                raise ValidationError(
                    "high_fanout threshold must be >= 1",
                    details={"rule_id": r.rule_id},
                )
        elif r.rule_type is RuleType.LOOP_DETECT:
            pass


def _num(p: dict, key: str, rule_id: str) -> float:
    v = p.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValidationError(
            f"{key} must be a number", details={"rule_id": rule_id}
        )
    return float(v)


def _matches(r: Rule, tx: Transaction, graph: GraphSnapshot) -> bool:
    p = r.params
    if r.rule_type is RuleType.AMOUNT_OVER:
        return tx.amount >= float(p["threshold"])
    if r.rule_type is RuleType.ADDRESS_BLACKLIST:
        addrs = p["addresses"]
        return tx.frm in addrs or tx.to in addrs
    if r.rule_type is RuleType.HIGH_FANOUT:
        return graph.fanout(tx.tx_id) >= float(p["threshold"])
    if r.rule_type is RuleType.LOOP_DETECT:
        return graph.is_loop(tx.tx_id)
    return False


def evaluate_tx(
    rules: tuple[Rule, ...] | list[Rule],
    tx: Transaction,
    graph: GraphSnapshot,
) -> TxResult:
    hits: list[RuleHit] = []
    for r in rules:
        if _matches(r, tx, graph):
            hits.append(RuleHit(rule_id=r.rule_id, score=r.score, tags=r.tags))
    hits.sort(key=lambda h: h.rule_id)
    tags = tuple(sorted({t for h in hits for t in h.tags}))
    score = sum(h.score for h in hits)
    return TxResult(
        tx_id=tx.tx_id,
        frm=tx.frm,
        to=tx.to,
        amount=tx.amount,
        score=score,
        tags=tags,
        hits=tuple(hits),
        risk=bool(hits),
    )


def evaluate(
    rules: tuple[Rule, ...] | list[Rule],
    transactions: list[Transaction] | tuple[Transaction, ...],
    graph: GraphSnapshot,
) -> dict[str, TxResult]:
    validate_rules(rules)
    return {
        tx.tx_id: evaluate_tx(rules, tx, graph)
        for tx in sorted(transactions, key=lambda t: (t.ts, t.tx_id))
    }
