"""Domain and API models (type definitions only; no behavior here)."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Transactions / batches
# ---------------------------------------------------------------------------
class Transaction(BaseModel):
    tx_id: str
    from_addr: str
    to_addr: str
    asset: str
    amount: float
    timestamp: int  # epoch seconds


class ImportBatchRequest(BaseModel):
    batch_id: str
    transactions: list[Transaction] = Field(default_factory=list)


class ImportBatchResponse(BaseModel):
    batch_id: str
    data_version: int
    imported: int
    duplicates: int
    replayed: bool  # True when this batch_id was already imported (idempotent replay)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
class RuleCondition(BaseModel):
    min_amount: float | None = None
    max_amount: float | None = None
    assets: list[str] | None = None
    from_addrs: list[str] | None = None
    to_addrs: list[str] | None = None
    min_fan_out: int | None = None           # out-degree of from_addr
    min_fan_in: int | None = None            # in-degree of to_addr
    in_cycle: bool | None = None             # either endpoint sits on a cycle


class Rule(BaseModel):
    rule_id: str
    name: str = ""
    description: str = ""
    condition: RuleCondition = Field(default_factory=RuleCondition)
    score: int = 0
    labels: list[str] = Field(default_factory=list)


class PublishRulesRequest(BaseModel):
    rules: list[Rule]
    note: str = ""


class RollbackRequest(BaseModel):
    version: int


class RuleVersionInfo(BaseModel):
    version: int
    rule_count: int
    note: str
    created_at: float
    active: bool


# ---------------------------------------------------------------------------
# Risk evaluation results
# ---------------------------------------------------------------------------
class RuleHit(BaseModel):
    rule_id: str
    score: int
    labels: list[str]


class TxRiskResult(BaseModel):
    tx_id: str
    score: int
    labels: list[str]
    hits: list[RuleHit]


class RiskQueryResponse(BaseModel):
    data_version: int
    rule_version: int
    results: list[TxRiskResult]


# ---------------------------------------------------------------------------
# Dry-run (rule trial evaluation)
# ---------------------------------------------------------------------------
class BaselineSpec(BaseModel):
    """Which rule set to compare against: current active, a historical rule
    version, or the rule set that was active at a historical point in time."""
    type: str = "current"            # current | version | as_of
    rule_version: int | None = None
    as_of: float | None = None


class DataScope(BaseModel):
    """Which data snapshot to evaluate on, plus optional narrowing."""
    type: str = "current"            # current | version | as_of
    data_version: int | None = None
    as_of: float | None = None
    tx_ids: list[str] | None = None
    limit: int | None = None


class DryRunRequest(BaseModel):
    candidate_rules: list[Rule]
    baseline: BaselineSpec = Field(default_factory=BaselineSpec)
    data_scope: DataScope = Field(default_factory=DataScope)
    max_diffs: int | None = None     # cap on returned diff entries


class EvalSide(BaseModel):
    """One side (candidate or baseline) of a per-transaction comparison."""
    score: int
    labels: list[str]
    hits: list[RuleHit]


class DiffEntry(BaseModel):
    tx_id: str
    change_type: str                 # added | removed | changed
    candidate: EvalSide
    baseline: EvalSide


class Coverage(BaseModel):
    full: bool
    scanned: int
    total: int
    reason: str | None = None        # e.g. SCAN_LIMIT when partial


class DryRunSummary(BaseModel):
    evaluated: int
    candidate_hits: int
    baseline_hits: int
    added: int
    removed: int
    changed: int


class DryRunResponse(BaseModel):
    data_version: int
    baseline_rule_version: int
    summary: DryRunSummary
    diffs: list[DiffEntry]
    diffs_truncated: bool
    coverage: Coverage
