"""In-memory, MVCC-style append-only store with a single global lock.

Design
------
* Every mutating operation publishes a **new immutable Snapshot** under the
  store lock and bumps ``data_version`` (transactions/relations) or the
  rule-set version. A reader takes one snapshot and never sees a torn
  state: each query result originates from one complete version.
* Rule sets are append-only. Every publish / rollback creates a new
  rule-set version containing the full list of rules active at that
  moment, so an explicit ``rule_set_version`` always reproduces the exact
  historical conclusion. Rules absent from a set are simply inactive;
  rollback republishes the previous definition as a *new* rule version
  (monotonic, never overwritten).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from .models import BatchStatus, ItemResult, ItemStatus
from .validation import NormalizedRelation, NormalizedTx


@dataclass(frozen=True)
class StoredTx:
    tx_hash: str
    from_addr: str
    to_addr: str
    amount: float
    ts_ms: int
    batch_id: str
    data_version: int
    commit_ms: int


@dataclass(frozen=True)
class StoredBatch:
    batch_id: str
    payload_fingerprint: str
    status: BatchStatus
    data_version: int
    rule_set_version: int
    accepted: int
    duplicate: int
    rejected: int
    items: tuple[ItemResult, ...]


@dataclass(frozen=True)
class RuleDef:
    rule_id: str
    version: int
    kind: str
    name: str
    description: str
    params: dict
    score: int
    tag: str
    effective_at: int
    rule_set_version: int


@dataclass(frozen=True)
class RuleSet:
    version: int
    published_at: int
    rules: tuple[RuleDef, ...]


@dataclass(frozen=True)
class Snapshot:
    data_version: int
    transactions: dict[str, StoredTx]
    outgoing: dict[str, frozenset[str]]
    incoming: dict[str, frozenset[str]]
    relation_kind: dict[tuple[str, str], str]
    batches: dict[str, StoredBatch]
    rule_set: RuleSet


_EMPTY_RULESET = RuleSet(version=0, published_at=0, rules=())


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        self._data_version = 0
        self._txs: dict[str, StoredTx] = {}
        self._outgoing: dict[str, set[str]] = {}
        self._incoming: dict[str, set[str]] = {}
        self._relation_kind: dict[tuple[str, str], str] = {}
        self._batches: dict[str, StoredBatch] = {}
        self._rulesets: list[RuleSet] = [_EMPTY_RULESET]
        self._current_rule: dict[str, RuleDef] = {}

    # ---- snapshots --------------------------------------------------------
    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                data_version=self._data_version,
                transactions=dict(self._txs),
                outgoing={k: frozenset(v) for k, v in self._outgoing.items()},
                incoming={k: frozenset(v) for k, v in self._incoming.items()},
                relation_kind=dict(self._relation_kind),
                batches=dict(self._batches),
                rule_set=self._rulesets[-1],
            )

    # ---- batches & transactions ------------------------------------------
    def commit_transactions(self, accepted_txs: list[NormalizedTx],
                            batch_id: str, payload_fingerprint: str,
                            item_results: list[ItemResult],
                            rule_set_version: int,
                            commit_ms: int) -> tuple[int, StoredBatch]:
        with self._lock:
            existing = self._batches.get(batch_id)
            if existing is not None:
                return self._data_version, existing
            self._data_version += 1
            dv = self._data_version
            for tx in accepted_txs:
                self._txs[tx.tx_hash] = StoredTx(
                    tx_hash=tx.tx_hash, from_addr=tx.from_addr, to_addr=tx.to_addr,
                    amount=tx.amount, ts_ms=tx.ts_ms, batch_id=batch_id,
                    data_version=dv, commit_ms=commit_ms,
                )
            n_acc = sum(1 for i in item_results if i.status == ItemStatus.ACCEPTED)
            n_dup = sum(1 for i in item_results if i.status == ItemStatus.DUPLICATE)
            n_rej = sum(1 for i in item_results if i.status == ItemStatus.REJECTED)
            if n_acc and (n_rej or n_dup):
                status = BatchStatus.PARTIAL
            elif n_acc:
                status = BatchStatus.ACCEPTED
            else:
                status = BatchStatus.REJECTED
            stored = StoredBatch(
                batch_id=batch_id, payload_fingerprint=payload_fingerprint,
                status=status, data_version=dv, rule_set_version=rule_set_version,
                accepted=n_acc, duplicate=n_dup, rejected=n_rej,
                items=tuple(item_results),
            )
            self._batches[batch_id] = stored
            return dv, stored

    def commit_relations(self, accepted: list[NormalizedRelation]) -> int:
        with self._lock:
            self._data_version += 1
            dv = self._data_version
            for rel in accepted:
                self._outgoing.setdefault(rel.from_addr, set()).add(rel.to_addr)
                self._incoming.setdefault(rel.to_addr, set()).add(rel.from_addr)
                if rel.kind is not None:
                    self._relation_kind[(rel.from_addr, rel.to_addr)] = rel.kind
            return dv

    # ---- rule sets --------------------------------------------------------
    def publish_ruleset(self, desired: list[dict], published_at: int) -> RuleSet:
        """Publish a new rule-set version containing exactly ``desired``.

        Rules missing from ``desired`` become inactive (they remain visible
        in older rule-set versions). A changed rule definition becomes a
        new per-rule version; an identical definition keeps its version.
        """
        with self._lock:
            new_version = len(self._rulesets)
            prev_by_id = {r.rule_id: r for r in self._rulesets[-1].rules}
            new_rules: list[RuleDef] = []
            for d in desired:
                old = prev_by_id.get(d["rule_id"])
                if old is not None and _same_rule(old, d):
                    new_rules.append(_with_ruleset(old, new_version))
                else:
                    # rollback republishes an older definition: find its
                    # highest historical version so versions stay monotonic
                    version = old.version + 1 if old is not None else 1
                    if old is None:
                        for rs in reversed(self._rulesets[:-1]):
                            for r in rs.rules:
                                if r.rule_id == d["rule_id"]:
                                    version = max(version, r.version + 1)
                    new_rules.append(RuleDef(
                        rule_id=d["rule_id"], version=version, kind=d["kind"],
                        name=d["name"], description=d["description"],
                        params=d["params"], score=d["score"], tag=d["tag"],
                        effective_at=d["effective_at"],
                        rule_set_version=new_version,
                    ))
            new_rules.sort(key=lambda r: r.rule_id)
            rs = RuleSet(version=new_version, published_at=published_at,
                         rules=tuple(new_rules))
            self._rulesets.append(rs)
            self._current_rule = {r.rule_id: r for r in new_rules}
            return rs

    def ruleset(self, version: int | None = None) -> RuleSet:
        with self._lock:
            if version is None:
                return self._rulesets[-1]
            if version < 0 or version >= len(self._rulesets):
                raise KeyError(version)
            return self._rulesets[version]

    def ruleset_as_of(self, as_of_ms: int) -> RuleSet:
        """Newest rule set published at or before ``as_of_ms`` (v0 eligible)."""
        with self._lock:
            chosen = self._rulesets[0]
            for rs in self._rulesets:
                if rs.published_at <= as_of_ms:
                    chosen = rs
                else:
                    break
            return chosen

    def all_rulesets(self) -> list[RuleSet]:
        with self._lock:
            return list(self._rulesets)

    def current_rule(self, rule_id: str) -> RuleDef | None:
        with self._lock:
            return self._current_rule.get(rule_id)

    def rule_version(self, rule_id: str, version: int) -> RuleDef | None:
        with self._lock:
            for rs in reversed(self._rulesets):
                for r in rs.rules:
                    if r.rule_id == rule_id and r.version == version:
                        return r
            return None


def _with_ruleset(old: RuleDef, new_version: int) -> RuleDef:
    return RuleDef(
        rule_id=old.rule_id, version=old.version, kind=old.kind,
        name=old.name, description=old.description, params=old.params,
        score=old.score, tag=old.tag, effective_at=old.effective_at,
        rule_set_version=new_version,
    )


def _same_rule(old: RuleDef, d: dict) -> bool:
    return (old.kind == d["kind"] and old.name == d["name"]
            and old.description == d["description"]
            and old.params == d["params"] and old.score == d["score"]
            and old.tag == d["tag"] and old.effective_at == d["effective_at"])
