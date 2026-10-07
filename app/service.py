"""Application service: import / publish / rollback / risk query.

All official reads resolve their (data snapshot, rule version) as one unit:
``as_of`` queries take the data prefix and the rule version that were both
current at that instant, so historical answers are self-consistent. Explicit
version pins are also honoured.
"""

from __future__ import annotations

from collections.abc import Callable

from app.engine import GraphSnapshot, evaluate
from app.errors import NotFoundError, ValidationError
from app.models import (
    AddressRiskOut,
    DataScope,
    Edge,
    ImportBatch,
    ImportBatchIn,
    ImportResponse,
    PublishResponse,
    RollbackResponse,
    Rule,
    RuleIn,
    RuleSetVersion,
    Transaction,
    TxResult,
    TxResultOut,
)
from app.store import Store


class Snapshot:
    """An immutable data prefix loaded for lock-free evaluation."""

    def __init__(
        self,
        version: int,
        transactions: list[Transaction],
        edges: list[Edge],
        tx_filter: frozenset[str] | None = None,
    ) -> None:
        self.version = version
        self.edges = edges
        all_tx = sorted(transactions, key=lambda t: (t.ts, t.tx_id))
        self.total_count = len(all_tx)
        self.all_tx_ids = frozenset(t.tx_id for t in all_tx)
        self.tx_filter = tx_filter
        self.transactions = (
            [t for t in all_tx if t.tx_id in tx_filter] if tx_filter is not None else all_tx
        )
        # Graph features are always computed over the *whole* prefix, so a
        # tx-scoped query still sees true fanout/cycle structure.
        self.graph = GraphSnapshot(all_tx, edges)


class RiskService:
    def __init__(
        self,
        store: Store | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store or Store(clock=clock)

    # ------------------------------------------------------------------ #
    # Import
    # ------------------------------------------------------------------ #
    def import_batch(self, payload: ImportBatchIn) -> ImportResponse:
        txs: list[Transaction] = []
        seen_in_batch: set[str] = set()
        for t in payload.transactions:
            if t.tx_id in seen_in_batch:
                raise ValidationError(
                    "duplicate tx_id within batch", details={"tx_id": t.tx_id}
                )
            seen_in_batch.add(t.tx_id)
            txs.append(Transaction(t.tx_id, t.frm, t.to, t.amount, t.ts))

        edges = tuple(Edge(e.frm, e.to) for e in payload.edges)
        imported_at = payload.imported_at
        if imported_at is None:
            imported_at = self.store.now_us()
        batch = ImportBatch(
            batch_id=payload.batch_id,
            transactions=tuple(txs),
            edges=edges,
            imported_at=imported_at,
        )
        deduped, dv = self.store.import_batch(batch)
        return ImportResponse(
            batch_id=payload.batch_id,
            imported=not deduped,
            deduped=deduped,
            data_version=dv,
        )

    # ------------------------------------------------------------------ #
    # Rule lifecycle
    # ------------------------------------------------------------------ #
    def publish_rules(
        self,
        rules_in: list[RuleIn],
        note: str = "",
        created_at: int | None = None,
    ) -> PublishResponse:
        if not rules_in:
            raise ValidationError("at least one rule is required")
        rules = tuple(Rule.from_in(r) for r in rules_in)
        from app.engine import validate_rules

        validate_rules(rules)
        rs = self.store.publish_rules(rules, note=note, created_at=created_at)
        return PublishResponse(
            version=rs.version, rule_count=len(rs.rules), created_at=rs.created_at
        )

    def rollback(self, version: int, note: str = "") -> RollbackResponse:
        new = self.store.rollback_to(version, note=note)
        return RollbackResponse(version=new.version, rolled_back_to=version)

    # ------------------------------------------------------------------ #
    # Snapshot / version resolution
    # ------------------------------------------------------------------ #
    def resolve_data_version(self, scope: DataScope | None) -> int:
        scope = scope or DataScope()
        if scope.kind == "current":
            return self.store.data_version_count()
        if scope.kind == "data_version":
            if scope.data_version is None:
                raise ValidationError("data_version is required")
            n = scope.data_version
            self.store.get_data_version(n)  # raises if unknown
            return n
        # as_of
        if scope.as_of is None:
            raise ValidationError("as_of is required")
        return self.store.resolve_data_version_as_of(scope.as_of).version

    def load_snapshot(self, scope: DataScope | None = None) -> Snapshot:
        scope = scope or DataScope()
        n = self.resolve_data_version(scope)
        batches = self.store.batches_prefix(n)
        txs: list[Transaction] = []
        edges: list[Edge] = []
        for b in batches:
            txs.extend(b.transactions)
            edges.extend(b.edges)
        tx_filter = frozenset(scope.tx_ids) if scope.tx_ids is not None else None
        return Snapshot(n, txs, edges, tx_filter)

    def resolve_baseline_rules(
        self,
        kind: str = "current",
        rule_version: int | None = None,
        as_of: int | None = None,
    ) -> tuple[tuple[Rule, ...], int]:
        if kind == "rule_version":
            if rule_version is None:
                raise ValidationError("rule_version is required")
            rs = self.store.get_rule_version(rule_version)
            return rs.rules, rs.version
        if kind == "as_of":
            if as_of is None:
                raise ValidationError("as_of is required")
            rs = self.store.resolve_rule_version_as_of(as_of)
            if rs is None:
                return (), 0
            return rs.rules, rs.version
        # current
        rs = self.store.current_rule_version()
        if rs is None:
            return (), 0
        return rs.rules, rs.version

    # ------------------------------------------------------------------ #
    # Official risk queries
    # ------------------------------------------------------------------ #
    def _evaluate_official(
        self,
        scope: DataScope | None,
        rule_version: int | None = None,
        as_of: int | None = None,
    ) -> tuple[Snapshot, dict[str, TxResult], int]:
        if as_of is not None and scope is None:
            # Historical-point convenience: data + rules from the same instant.
            scope = DataScope(kind="as_of", as_of=as_of)
        snap = self.load_snapshot(scope)
        if rule_version is not None:
            rules, rv = self.resolve_baseline_rules(
                kind="rule_version", rule_version=rule_version
            )
        elif scope is not None and scope.kind == "as_of" and scope.as_of is not None:
            rules, rv = self.resolve_baseline_rules(
                kind="as_of", as_of=scope.as_of
            )
        elif as_of is not None:
            rules, rv = self.resolve_baseline_rules(kind="as_of", as_of=as_of)
        else:
            rules, rv = self.resolve_baseline_rules(kind="current")
        results = evaluate(rules, snap.transactions, snap.graph)
        return snap, results, rv

    def query_tx(
        self,
        tx_id: str,
        scope: DataScope | None = None,
        rule_version: int | None = None,
        as_of: int | None = None,
    ) -> TxResultOut:
        snap, results, rv = self._evaluate_official(scope, rule_version, as_of)
        res = results.get(tx_id)
        if res is None:
            # Distinguish "unknown transaction" from "not risky / absent then".
            if not any(t.tx_id == tx_id for t in snap.transactions):
                raise NotFoundError("transaction not found", details={"tx_id": tx_id})
            res = TxResult(tx_id, "", "", 0.0, 0, (), (), False)
        return TxResultOut(
            tx_id=res.tx_id,
            score=res.score,
            tags=list(res.tags),
            hit_rule_ids=[h.rule_id for h in res.hits],
            risk=res.risk,
            rule_version=rv,
            data_version=snap.version,
        )

    def query_address(
        self,
        address: str,
        scope: DataScope | None = None,
        rule_version: int | None = None,
        as_of: int | None = None,
    ) -> AddressRiskOut:
        snap, results, rv = self._evaluate_official(scope, rule_version, as_of)
        involved = [
            t.tx_id
            for t in snap.transactions
            if t.frm == address or t.to == address
        ]
        if not involved:
            raise NotFoundError("address has no transactions", details={"address": address})
        risky = [results[i] for i in involved if results[i].risk]
        tags = sorted({tag for r in risky for tag in r.tags})
        score = max((r.score for r in risky), default=0)
        return AddressRiskOut(
            address=address,
            risk=bool(risky),
            score=score,
            tags=tags,
            tx_ids=sorted(t.tx_id for t in risky),
            rule_version=rv,
            data_version=snap.version,
        )

    def current_rule_version(self) -> RuleSetVersion | None:
        return self.store.current_rule_version()
