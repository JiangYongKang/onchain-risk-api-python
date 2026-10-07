"""Application service: orchestrates store + engine, enforces limits."""
from __future__ import annotations

from app.models import (
    DryRunRequest,
    DryRunResponse,
    ImportBatchResponse,
    RiskQueryResponse,
    RuleVersionInfo,
)
from app.storage import Store

# Capacity limits (stable, documented; exceeded -> DRY_RUN_LIMIT_EXCEEDED)
MAX_CANDIDATE_RULES = 50
MAX_DRYRUN_TXS = 20_000
MAX_DRYRUN_SCAN = 50_000   # beyond this, results are marked as partial coverage


class RiskService:
    def __init__(self, store: Store | None = None) -> None:
        self._store = store or Store()
        self._eval_cache: dict = {}

    # -- imports -------------------------------------------------------------
    def import_batch(self, batch_id: str, transactions) -> ImportBatchResponse:
        raise NotImplementedError

    # -- rule lifecycle --------------------------------------------------------
    def publish_rules(self, rules, note: str = "") -> RuleVersionInfo:
        raise NotImplementedError

    def rollback_rules(self, version: int) -> RuleVersionInfo:
        raise NotImplementedError

    def list_rule_versions(self) -> list[RuleVersionInfo]:
        raise NotImplementedError

    # -- risk queries ----------------------------------------------------------
    def query_risk(self, data_version: int | None = None,
                   rule_version: int | None = None,
                   as_of: float | None = None) -> RiskQueryResponse:
        raise NotImplementedError

    # -- dry-run ---------------------------------------------------------------
    def dry_run(self, request: DryRunRequest) -> DryRunResponse:
        raise NotImplementedError
