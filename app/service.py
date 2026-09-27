"""Application service: batches, rules and risk queries.

Concurrency model
-----------------
All mutating operations take ``store._lock`` via the Store methods; queries
take one immutable :class:`Snapshot`. Because commit + duplicate-check is a
single critical section, concurrent submission of the same / overlapping
batch cannot double-count, and every read observes one complete version.

Version / time semantics
------------------------
* ``rule_set_version`` pins the exact rule set (reproduces history).
* ``as_of_ms`` selects the newest rule set published at or before that
  time AND only transactions visible at that time.
* Both may be combined; by default the current data and current rule set
  are used.
"""
from __future__ import annotations

import hashlib
import json
import threading

from . import limits
from .clock import Clock
from .errors import (
    ASOF_INVALID,
    BATCH_EMPTY,
    BATCH_ID_MISSING,
    BATCH_REPLAY_MISMATCH,
    BATCH_TOO_LARGE,
    RELATION_BATCH_TOO_LARGE,
    RULE_NOT_FOUND,
    RULE_VERSION_NOT_FOUND,
    TX_HASH_CONFLICT,
    TX_NOT_FOUND,
    VERSION_NOT_FOUND,
    ServiceError,
)
from .graph import traverse
from .logging_config import log_event
from .models import (
    BatchStatusResult,
    BatchSubmitResult,
    ItemResult,
    ItemStatus,
    RelationResult,
    RiskResult,
    RuleInfo,
    RulePublishResult,
    RuleRollbackResult,
    RuleSetInfo,
)
from .rules import evaluate
from .store import Store
from .validation import validate_relations, validate_rule, validate_transactions


class RiskService:
    def __init__(self, clock: Clock | None = None, store: Store | None = None):
        self.clock = clock or Clock()
        self.store = store or Store()
        # serializes mutating API operations on top of the store lock, so a
        # publish cannot interleave with the validation -> commit window
        self._write_lock = threading.Lock()

    # ======================================================================
    # transaction batches
    # ======================================================================
    def submit_batch(self, raw: dict) -> BatchSubmitResult:
        if not isinstance(raw, dict):
            raise ServiceError("BAD_REQUEST", "request body must be an object",
                               status_code=400)
        batch_id = raw.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ServiceError(BATCH_ID_MISSING, "batch_id is required")
        batch_id = batch_id.strip()
        txs = raw.get("transactions")
        if not isinstance(txs, list) or not txs:
            raise ServiceError(BATCH_EMPTY, "transactions must be a non-empty list")
        if len(txs) > limits.MAX_BATCH_ITEMS:
            raise ServiceError(
                BATCH_TOO_LARGE,
                f"batch has {len(txs)} items; hard limit is {limits.MAX_BATCH_ITEMS}")

        fingerprint = _fingerprint(txs)
        with self._write_lock:
            snap = self.store.snapshot()
            existing = snap.batches.get(batch_id)
            if existing is not None:
                if existing.payload_fingerprint != fingerprint:
                    raise ServiceError(
                        BATCH_REPLAY_MISMATCH,
                        f"batch_id {batch_id} already exists with a different payload",
                        status_code=409,
                        details={"existing_data_version": existing.data_version})
                log_event("batch_replay", batch_id=batch_id,
                          data_version=existing.data_version,
                          rule_set_version=existing.rule_set_version,
                          status=existing.status.value)
                return BatchSubmitResult(
                    batch_id=batch_id, replayed=True, status=existing.status,
                    data_version=existing.data_version,
                    rule_set_version=existing.rule_set_version,
                    accepted=existing.accepted, duplicate=existing.duplicate,
                    rejected=existing.rejected, items=list(existing.items))

            validated = validate_transactions(txs, self.clock.now_ms())
            items = list(validated.items)

            # cross-batch idempotency / conflict against already-stored txs
            accepted_rows = []
            for norm in validated.accepted:
                stored = snap.transactions.get(norm.tx_hash)
                if stored is None:
                    accepted_rows.append(norm)
                    continue
                same = (stored.from_addr == norm.from_addr
                        and stored.to_addr == norm.to_addr
                        and stored.amount == norm.amount
                        and stored.ts_ms == norm.ts_ms)
                if same:
                    items[norm.index] = ItemResult(
                        index=norm.index, tx_hash=norm.tx_hash,
                        status=ItemStatus.DUPLICATE,
                        error="TX_HASH_DUPLICATE",
                        message=(f"already imported in batch "
                                 f"{stored.batch_id}; not counted again"))
                else:
                    items[norm.index] = ItemResult(
                        index=norm.index, tx_hash=norm.tx_hash,
                        status=ItemStatus.REJECTED, error=TX_HASH_CONFLICT,
                        message=("same tx_hash stored with different fields; "
                                 "contradictory data rejected"))

            rsv = snap.rule_set.version
            dv, stored_batch = self.store.commit_transactions(
                accepted_rows, batch_id, fingerprint, items, rsv,
                self.clock.now_ms())

            log_event("batch_submit", batch_id=batch_id, data_version=dv,
                      rule_set_version=rsv, status=stored_batch.status.value,
                      accepted=stored_batch.accepted,
                      duplicate=stored_batch.duplicate,
                      rejected=stored_batch.rejected,
                      rejections=_rejection_summary(items))
            return BatchSubmitResult(
                batch_id=batch_id, replayed=False, status=stored_batch.status,
                data_version=dv, rule_set_version=rsv,
                accepted=stored_batch.accepted, duplicate=stored_batch.duplicate,
                rejected=stored_batch.rejected, items=items)

    def get_batch(self, batch_id: str) -> BatchStatusResult:
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ServiceError(BATCH_ID_MISSING, "batch_id is required")
        b = self.store.snapshot().batches.get(batch_id.strip())
        if b is None:
            return BatchStatusResult(batch_id=batch_id.strip(), found=False)
        return BatchStatusResult(
            batch_id=b.batch_id, found=True, replayed=True, status=b.status,
            data_version=b.data_version, rule_set_version=b.rule_set_version,
            accepted=b.accepted, duplicate=b.duplicate, rejected=b.rejected,
            items=list(b.items))

    # ======================================================================
    # address relations
    # ======================================================================
    def import_relations(self, raw: dict) -> RelationResult:
        if not isinstance(raw, dict) or not isinstance(raw.get("relations"), list):
            raise ServiceError("BAD_REQUEST",
                               "body must be an object with a 'relations' list",
                               status_code=400)
        rels = raw["relations"]
        if len(rels) > limits.MAX_RELATION_BATCH:
            raise ServiceError(
                RELATION_BATCH_TOO_LARGE,
                f"relation batch has {len(rels)} edges; hard limit "
                f"{limits.MAX_RELATION_BATCH}")
        accepted, errors = validate_relations(rels)
        with self._write_lock:
            dv = self.store.commit_relations(accepted)
        log_event("relations_import", data_version=dv,
                  accepted=len(accepted), rejected=len(errors))
        return RelationResult(accepted=len(accepted), rejected=len(errors),
                              errors=errors, data_version=dv)

    # ======================================================================
    # rules
    # ======================================================================
    def publish_rule(self, raw: dict) -> RulePublishResult:
        normalized = validate_rule(raw)
        now = self.clock.now_ms()
        effective_at = normalized["effective_at"] or now
        normalized["effective_at"] = effective_at
        with self._write_lock:
            snap = self.store.snapshot()
            desired = [_rule_to_dict(r) for r in snap.rule_set.rules
                       if r.rule_id != normalized["rule_id"]]
            desired.append(normalized)
            rs = self.store.publish_ruleset(desired, now)
        rule = next(r for r in rs.rules if r.rule_id == normalized["rule_id"])
        log_event("rule_publish", rule_id=rule.rule_id,
                  rule_version=rule.version, rule_set_version=rs.version,
                  effective_at=effective_at)
        return RulePublishResult(
            rule_id=rule.rule_id, rule_version=rule.version,
            rule_set_version=rs.version, published_at=now,
            effective_at=effective_at)

    def rollback_rule(self, rule_id: str, target_version: int) -> RuleRollbackResult:
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ServiceError(RULE_NOT_FOUND, "rule_id is required")
        rule_id = rule_id.strip()
        if not isinstance(target_version, int) or isinstance(target_version, bool):
            raise ServiceError(RULE_VERSION_NOT_FOUND,
                               "target_version must be an integer", status_code=400)
        now = self.clock.now_ms()
        with self._write_lock:
            target = self.store.rule_version(rule_id, target_version)
            if target is None:
                raise ServiceError(
                    RULE_VERSION_NOT_FOUND,
                    f"rule {rule_id} version {target_version} does not exist")
            snap = self.store.snapshot()
            desired = [_rule_to_dict(r) for r in snap.rule_set.rules
                       if r.rule_id != rule_id]
            desired.append({
                "rule_id": target.rule_id, "kind": target.kind,
                "name": target.name, "description": target.description,
                "params": target.params, "score": target.score,
                "tag": target.tag, "effective_at": now,
            })
            rs = self.store.publish_ruleset(desired, now)
        new_rule = next(r for r in rs.rules if r.rule_id == rule_id)
        log_event("rule_rollback", rule_id=rule_id,
                  from_rule_version=_current_version(snap, rule_id),
                  to_rule_version=new_rule.version,
                  restored_from=target_version, rule_set_version=rs.version)
        return RuleRollbackResult(
            rule_id=rule_id,
            from_rule_version=_current_version(snap, rule_id),
            to_rule_version=new_rule.version, rule_set_version=rs.version,
            rolled_back_at=now, effective_at=now)

    def get_ruleset(self, version: int | None = None) -> RuleSetInfo:
        try:
            rs = self.store.ruleset(version)
        except KeyError:
            raise ServiceError(VERSION_NOT_FOUND,
                               f"rule_set_version {version} does not exist")
        return RuleSetInfo(rule_set_version=rs.version,
                           published_at=rs.published_at,
                           rules=[_rule_info(r) for r in rs.rules])

    # ======================================================================
    # risk
    # ======================================================================
    def risk(self, tx_hash: str, *,
             rule_set_version: int | None = None,
             as_of_ms: int | None = None) -> RiskResult:
        if not isinstance(tx_hash, str) or not tx_hash.strip():
            raise ServiceError(TX_NOT_FOUND, "tx_hash is required", status_code=400)
        tx_hash = tx_hash.strip().lower()
        if as_of_ms is not None and (not isinstance(as_of_ms, int)
                                     or isinstance(as_of_ms, bool) or as_of_ms <= 0):
            raise ServiceError(ASOF_INVALID,
                               "as_of_ms must be a positive epoch-ms integer")

        snap = self.store.snapshot()
        tx = snap.transactions.get(tx_hash)

        # Historical visibility: a transaction committed after as_of did not
        # exist at that point in time (rule evaluation sees only data that
        # had been committed then).
        visible_txs = snap.transactions
        if as_of_ms is not None:
            visible_txs = {
                h: t for h, t in snap.transactions.items()
                if t.commit_ms <= as_of_ms
            }
        if tx is not None and as_of_ms is not None and tx.commit_ms > as_of_ms:
            tx = None

        if tx is None:
            log_event("risk_missing", tx_hash=tx_hash)
            return RiskResult(tx_hash=tx_hash, found=False,
                              data_version=snap.data_version,
                              rule_set_version=snap.rule_set.version)

        # select rule set: explicit pin wins; as_of picks published-at-time
        if rule_set_version is not None:
            try:
                ruleset = self.store.ruleset(rule_set_version)
            except KeyError:
                raise ServiceError(VERSION_NOT_FOUND,
                                   f"rule_set_version {rule_set_version} not found")
        elif as_of_ms is not None:
            ruleset = self.store.ruleset_as_of(as_of_ms)
        else:
            ruleset = snap.rule_set

        historical_snap = _with_transactions(snap, visible_txs)
        graph_result = traverse(
            historical_snap.outgoing, historical_snap.incoming,
            historical_snap.relation_kind, tx.from_addr,
            max_fanout=limits.MAX_FANOUT, max_nodes=limits.MAX_GRAPH_NODES,
            max_depth=limits.MAX_GRAPH_DEPTH)
        evidence = evaluate(tx, ruleset, historical_snap, graph_result)
        score = min(100, sum(e.score for e in evidence))
        labels = sorted({e.tag for e in evidence})

        notes: list[str] = []
        if graph_result.truncated:
            notes.append(f"address graph result is partial: {graph_result.truncation_reason}")
        if graph_result.cycles:
            notes.append(f"relation cycle detected involving: {','.join(graph_result.cycles)}")
        if as_of_ms is not None or rule_set_version is not None:
            notes.append("historical query: result reflects the selected data/rule version")

        log_event(
            "risk_query", tx_hash=tx_hash, data_version=snap.data_version,
            rule_set_version=ruleset.version,
            pinned=rule_set_version, as_of_ms=as_of_ms,
            score=score, labels=labels,
            hits=[{"rule_id": e.rule_id, "rule_version": e.rule_version,
                   "tag": e.tag, "score": e.score} for e in evidence])

        return RiskResult(
            tx_hash=tx_hash, found=True, data_version=snap.data_version,
            rule_set_version=ruleset.version, as_of=as_of_ms,
            pinned_rule_set_version=rule_set_version,
            score=score, labels=labels, evidence=evidence,
            counterparties=graph_result.counterparties,
            graph_truncated=graph_result.truncated,
            graph_truncation_reason=graph_result.truncation_reason,
            notes=notes)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _fingerprint(txs: list) -> str:
    blob = json.dumps(txs, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _rejection_summary(items: list[ItemResult]) -> dict:
    summary: dict[str, int] = {}
    for i in items:
        if i.status == ItemStatus.REJECTED:
            summary[i.error] = summary.get(i.error, 0) + 1
    return summary


def _rule_to_dict(r) -> dict:
    return {"rule_id": r.rule_id, "kind": r.kind, "name": r.name,
            "description": r.description, "params": r.params, "score": r.score,
            "tag": r.tag, "effective_at": r.effective_at}


def _rule_info(r) -> RuleInfo:
    return RuleInfo(rule_id=r.rule_id, version=r.version, kind=r.kind,
                    name=r.name, description=r.description, params=r.params,
                    score=r.score, tag=r.tag, effective_at=r.effective_at)


def _current_version(snap, rule_id: str):
    for r in snap.rule_set.rules:
        if r.rule_id == rule_id:
            return r.version
    return None


def _with_transactions(snap, transactions):
    """Project a snapshot onto a different transaction index.

    Graph indexes are intentionally NOT filtered by time because relation
    edges are structural metadata without timestamps in this local model.
    """
    from dataclasses import replace
    return replace(snap, transactions=transactions)
