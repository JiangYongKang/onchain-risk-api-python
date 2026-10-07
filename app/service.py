"""Application service: orchestrates store + engine, enforces limits.

Isolation model: the store hands out immutable snapshot/ruleset references.
A dry-run pins its references up front and evaluates on them, so concurrent
imports / publishes / rollbacks never change what an in-flight dry-run (or
risk query) sees, and dry-runs never write anything back.
"""
from __future__ import annotations

import threading

from app import engine
from app.errors import DRY_RUN_LIMIT_EXCEEDED, VALIDATION_ERROR, ApiError
from app.models import (
    Coverage,
    DryRunRequest,
    DryRunResponse,
    DryRunSummary,
    ImportBatchResponse,
    RiskQueryResponse,
    Rule,
    RuleVersionInfo,
    Transaction,
)
from app.storage import RuleSet, Snapshot, Store

# Capacity limits (stable and documented; exceeded -> DRY_RUN_LIMIT_EXCEEDED)
MAX_CANDIDATE_RULES = 50      # max rules in one dry-run request
MAX_DRYRUN_TXS = 20_000       # max transactions evaluated in one dry-run
MAX_EVAL_TXS = 100_000        # max transactions evaluated in one risk query


class RiskService:
    def __init__(self, store: Store | None = None) -> None:
        self._store = store or Store()
        self._lock = threading.Lock()
        # Evaluation caches. Snapshots and rule sets are immutable, so
        # (data_version, rule_version) fully determines the result.
        self._features_cache: dict[int, engine.GraphFeatures] = {}
        self._eval_cache: dict[tuple[int, int], dict] = {}

    # -- imports -------------------------------------------------------------
    def import_batch(self, batch_id: str,
                     transactions: list[Transaction]) -> ImportBatchResponse:
        data_version, imported, duplicates, replayed = self._store.import_batch(
            batch_id, transactions)
        return ImportBatchResponse(batch_id=batch_id, data_version=data_version,
                                   imported=imported, duplicates=duplicates,
                                   replayed=replayed)

    # -- rule lifecycle --------------------------------------------------------
    def publish_rules(self, rules: list[Rule], note: str = "") -> RuleVersionInfo:
        ruleset = self._store.publish_rules(rules, note)
        return self._to_info(ruleset)

    def rollback_rules(self, version: int) -> RuleVersionInfo:
        return self._to_info(self._store.rollback_rules(version))

    def list_rule_versions(self) -> list[RuleVersionInfo]:
        return [self._to_info(rs) for rs in self._store.list_rule_versions()]

    def _to_info(self, rs: RuleSet) -> RuleVersionInfo:
        return RuleVersionInfo(
            version=rs.version, rule_count=len(rs.rules), note=rs.note,
            created_at=rs.created_at,
            active=rs.version == self._store.active_ruleset_version())

    # -- evaluation helpers ----------------------------------------------------
    def _features(self, snapshot: Snapshot) -> engine.GraphFeatures:
        with self._lock:
            cached = self._features_cache.get(snapshot.data_version)
        if cached is None:
            cached = engine.build_graph_features(snapshot.transactions)
            with self._lock:
                self._features_cache[snapshot.data_version] = cached
        return cached

    def _evaluate(self, snapshot: Snapshot, ruleset: RuleSet) -> dict:
        """Evaluate a versioned rule set on a snapshot (cached)."""
        key = (snapshot.data_version, ruleset.version)
        with self._lock:
            cached = self._eval_cache.get(key)
        if cached is None:
            cached = engine.evaluate_snapshot(
                self._features(snapshot), snapshot.transactions, ruleset.rules)
            with self._lock:
                self._eval_cache[key] = cached
        return cached

    def _evaluate_ad_hoc(self, snapshot: Snapshot,
                         transactions: tuple[Transaction, ...],
                         rules: tuple[Rule, ...]) -> dict:
        """Evaluate an unversioned (candidate) rule set. Uses the exact same
        engine as versioned evaluation, so publishing these rules later and
        querying the same data version yields identical results."""
        return engine.evaluate_snapshot(self._features(snapshot),
                                        transactions, rules)

    # -- risk queries ----------------------------------------------------------
    def query_risk(self, data_version: int | None = None,
                   rule_version: int | None = None,
                   as_of: float | None = None) -> RiskQueryResponse:
        snapshot, ruleset = self._resolve(data_version, rule_version, as_of)
        if len(snapshot.transactions) > MAX_EVAL_TXS:
            raise ApiError(
                DRY_RUN_LIMIT_EXCEEDED,
                f"data version {snapshot.data_version} has "
                f"{len(snapshot.transactions)} transactions, above the "
                f"evaluation limit of {MAX_EVAL_TXS}",
                status=422)
        results = self._evaluate(snapshot, ruleset)
        ordered = [results[tx.tx_id] for tx in snapshot.transactions]
        return RiskQueryResponse(data_version=snapshot.data_version,
                                 rule_version=ruleset.version, results=ordered)

    def _resolve(self, data_version: int | None, rule_version: int | None,
                 as_of: float | None) -> tuple[Snapshot, RuleSet]:
        """Resolve a self-consistent (snapshot, ruleset) pair.

        ``as_of`` pins BOTH data and rules to that point in time, so the
        answer always matches what the system would have concluded then; it
        cannot be mixed with explicit versions.
        """
        if as_of is not None:
            if data_version is not None or rule_version is not None:
                raise ApiError(
                    VALIDATION_ERROR,
                    "as_of cannot be combined with data_version/rule_version",
                    status=422)
            return (self._store.snapshot_as_of(as_of),
                    self._store.ruleset_as_of(as_of))
        snapshot = (self._store.snapshot_at_version(data_version)
                    if data_version is not None
                    else self._store.current_snapshot())
        ruleset = (self._store.ruleset_at_version(rule_version)
                   if rule_version is not None
                   else self._store.current_ruleset())
        return snapshot, ruleset

    # -- dry-run ---------------------------------------------------------------
    def dry_run(self, request: DryRunRequest) -> DryRunResponse:
        if not request.candidate_rules:
            raise ApiError(VALIDATION_ERROR,
                           "candidate_rules must not be empty", status=422)
        if len(request.candidate_rules) > MAX_CANDIDATE_RULES:
            raise ApiError(
                DRY_RUN_LIMIT_EXCEEDED,
                f"{len(request.candidate_rules)} candidate rules exceed the "
                f"limit of {MAX_CANDIDATE_RULES}", status=422)

        snapshot = self._resolve_scope_snapshot(request)
        baseline = self._resolve_baseline(request)

        matched = self._scope_transactions(snapshot, request)
        total = len(matched)
        limit = request.data_scope.limit
        if limit is not None and limit < 1:
            raise ApiError(VALIDATION_ERROR, "data_scope.limit must be >= 1",
                           status=422)
        if limit is None and total > MAX_DRYRUN_TXS:
            raise ApiError(
                DRY_RUN_LIMIT_EXCEEDED,
                f"data scope matches {total} transactions, above the dry-run "
                f"limit of {MAX_DRYRUN_TXS}; narrow the scope or set "
                f"data_scope.limit", status=422)

        full = limit is None or limit >= total
        scanned_txs = matched if full else tuple(matched[:limit])
        coverage = Coverage(
            full=full, scanned=len(scanned_txs), total=total,
            reason=None if full else "SCOPE_LIMIT")

        candidate_rules = tuple(request.candidate_rules)
        candidate = self._evaluate_ad_hoc(snapshot, scanned_txs, candidate_rules)
        baseline_res = self._evaluate_baseline(snapshot, baseline, scanned_txs)

        diffs = engine.diff_results(candidate, baseline_res)
        diffs_truncated = False
        if request.max_diffs is not None:
            if request.max_diffs < 1:
                raise ApiError(VALIDATION_ERROR, "max_diffs must be >= 1",
                               status=422)
            if len(diffs) > request.max_diffs:
                diffs = diffs[:request.max_diffs]
                diffs_truncated = True

        summary = DryRunSummary(
            evaluated=len(scanned_txs),
            candidate_hits=sum(1 for r in candidate.values() if r.hits),
            baseline_hits=sum(1 for r in baseline_res.values() if r.hits),
            added=sum(1 for d in diffs if d.change_type == "added"),
            removed=sum(1 for d in diffs if d.change_type == "removed"),
            changed=sum(1 for d in diffs if d.change_type == "changed"),
        )
        return DryRunResponse(data_version=snapshot.data_version,
                              baseline_rule_version=baseline.version,
                              summary=summary, diffs=diffs,
                              diffs_truncated=diffs_truncated,
                              coverage=coverage)

    def _evaluate_baseline(self, snapshot: Snapshot, baseline: RuleSet,
                           scanned_txs: tuple[Transaction, ...]) -> dict:
        # Full-scope, versioned baselines hit the shared cache; anything else
        # is computed ad hoc (still via the same engine).
        if scanned_txs == snapshot.transactions:
            return self._evaluate(snapshot, baseline)
        return engine.evaluate_snapshot(self._features(snapshot),
                                        scanned_txs, baseline.rules)

    def _resolve_scope_snapshot(self, request: DryRunRequest) -> Snapshot:
        scope = request.data_scope
        if scope.type == "current":
            return self._store.current_snapshot()
        if scope.type == "version":
            if scope.data_version is None:
                raise ApiError(VALIDATION_ERROR,
                               "data_scope.data_version is required for "
                               "type=version", status=422)
            return self._store.snapshot_at_version(scope.data_version)
        if scope.type == "as_of":
            if scope.as_of is None:
                raise ApiError(VALIDATION_ERROR,
                               "data_scope.as_of is required for type=as_of",
                               status=422)
            return self._store.snapshot_as_of(scope.as_of)
        raise ApiError(VALIDATION_ERROR,
                       f"unknown data_scope.type {scope.type!r}", status=422)

    def _resolve_baseline(self, request: DryRunRequest) -> RuleSet:
        spec = request.baseline
        if spec.type == "current":
            return self._store.current_ruleset()
        if spec.type == "version":
            if spec.rule_version is None:
                raise ApiError(VALIDATION_ERROR,
                               "baseline.rule_version is required for "
                               "type=version", status=422)
            return self._store.ruleset_at_version(spec.rule_version)
        if spec.type == "as_of":
            if spec.as_of is None:
                raise ApiError(VALIDATION_ERROR,
                               "baseline.as_of is required for type=as_of",
                               status=422)
            return self._store.ruleset_as_of(spec.as_of)
        raise ApiError(VALIDATION_ERROR,
                       f"unknown baseline.type {spec.type!r}", status=422)

    def _scope_transactions(self, snapshot: Snapshot,
                            request: DryRunRequest) -> tuple[Transaction, ...]:
        txs = snapshot.transactions
        tx_ids = request.data_scope.tx_ids
        if tx_ids is not None:
            wanted = set(tx_ids)
            txs = tuple(tx for tx in txs if tx.tx_id in wanted)
        return txs
