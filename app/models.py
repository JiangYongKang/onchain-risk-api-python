"""Shared domain models and API schemas.

Design overview
---------------
* Every rule publication creates an immutable ``RuleSetVersion`` (version id
  ``vN``); the published chain is append-only and can be rolled back by
  republishing an older version's rules (keeping the chain auditable).
* Imported transactions/edges are append-only; each import carries a client
  timestamp and we derive immutable "data versions" (``dN``) on demand, so an
  evaluation can always be pinned to one exact data snapshot + one exact rule
  version. That pin is what makes dry-run results reproducible after publish.
* Evaluation is deterministic: rule order is fixed, results are sorted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
class RuleType(StrEnum):
    AMOUNT_OVER = "amount_over"          # tx amount >= threshold
    ADDRESS_BLACKLIST = "address_blacklist"  # tx from/to a listed address
    HIGH_FANOUT = "high_fanout"          # from-address out-degree >= threshold
    LOOP_DETECT = "loop_detect"          # tx edge closes a directed cycle


RuleParamValue = float | int | str | list["float | int | str"]


class RuleIn(BaseModel):
    rule_id: str = Field(min_length=1)
    rule_type: RuleType
    score: int = Field(ge=0)
    tags: list[str] = Field(default_factory=list)
    # Params are intentionally permissive at the schema boundary; the engine
    # performs type/shape validation per rule type.
    params: dict[str, RuleParamValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    rule_type: RuleType
    score: int
    tags: tuple[str, ...]
    params: dict[str, RuleParamValue]

    @classmethod
    def from_in(cls, r: RuleIn) -> Rule:
        return cls(r.rule_id, r.rule_type, r.score, tuple(sorted(r.tags)), dict(r.params))


@dataclass(frozen=True)
class RuleSetVersion:
    version: int
    rules: tuple[Rule, ...]
    created_at: int  # epoch microseconds
    note: str
    parent_version: int | None = None


# --------------------------------------------------------------------------- #
# Import / data versions
# --------------------------------------------------------------------------- #
class TxIn(BaseModel):
    tx_id: str = Field(min_length=1)
    frm: str = Field(alias="from")
    to: str
    amount: float = Field(ge=0)
    ts: int  # transaction time, epoch microseconds

    model_config = {"populate_by_name": True}


class EdgeIn(BaseModel):
    frm: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


class ImportBatchIn(BaseModel):
    batch_id: str = Field(min_length=1)
    transactions: list[TxIn] = Field(default_factory=list)
    edges: list[EdgeIn] = Field(default_factory=list)
    imported_at: int | None = None  # client-controlled; server clock if absent


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    frm: str
    to: str
    amount: float
    ts: int


@dataclass(frozen=True)
class Edge:
    frm: str
    to: str


@dataclass(frozen=True)
class ImportBatch:
    batch_id: str
    transactions: tuple[Transaction, ...]
    edges: tuple[Edge, ...]
    imported_at: int


@dataclass(frozen=True)
class DataVersion:
    """Immutable snapshot of all data imported up to (and including) a point."""

    version: int
    as_of: int
    batches: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Evaluation results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    score: int
    tags: tuple[str, ...]


@dataclass(frozen=True)
class TxResult:
    tx_id: str
    frm: str
    to: str
    amount: float
    score: int
    tags: tuple[str, ...]
    hits: tuple[RuleHit, ...]
    risk: bool


# --------------------------------------------------------------------------- #
# Dry-run diff
# --------------------------------------------------------------------------- #
class DiffKind(StrEnum):
    NEW_HIT = "new_hit"          # candidate flags a tx the baseline does not
    LOST_HIT = "lost_hit"        # baseline flags a tx the candidate does not
    SCORE_CHANGED = "score_changed"
    TAGS_CHANGED = "tags_changed"
    HIT_RULES_CHANGED = "hit_rules_changed"  # same score/tags, other rules
    UNCHANGED = "unchanged"


class BaselineSpecKind(StrEnum):
    CURRENT = "current"
    RULE_VERSION = "rule_version"
    AS_OF = "as_of"


class BaselineSpec(BaseModel):
    kind: BaselineSpecKind = BaselineSpecKind.CURRENT
    rule_version: int | None = None
    as_of: int | None = None


class DataScope(BaseModel):
    kind: Literal["current", "as_of", "data_version"] = "current"
    as_of: int | None = None
    data_version: int | None = None
    tx_ids: list[str] | None = None  # optional sub-scope


class DryRunRequest(BaseModel):
    candidate_rules: list[RuleIn] = Field(min_length=1)
    baseline: BaselineSpec = Field(default_factory=BaselineSpec)
    scope: DataScope = Field(default_factory=DataScope)


class PublishRulesRequest(BaseModel):
    rules: list[RuleIn] = Field(min_length=1)
    note: str = ""
    created_at: int | None = None


class RollbackRequest(BaseModel):
    note: str = ""


class TxView(BaseModel):
    tx_id: str
    frm: str
    to: str
    amount: float
    score: int
    tags: list[str]
    hit_rule_ids: list[str]
    risk: bool


class HitContrib(BaseModel):
    rule_id: str
    score: int
    tags: list[str]


class TxDiff(BaseModel):
    tx_id: str
    kind: DiffKind
    baseline: TxView | None = None
    candidate: TxView | None = None
    candidate_contributors: list[HitContrib] = Field(default_factory=list)
    baseline_contributors: list[HitContrib] = Field(default_factory=list)
    added_tags: list[str] = Field(default_factory=list)
    removed_tags: list[str] = Field(default_factory=list)
    score_delta: int = 0


class DryRunResponse(BaseModel):
    data_version: int
    candidate_rule_count: int
    baseline_rule_version: int
    total_tx_count: int          # tx present in the pinned data version
    in_scope_tx_count: int       # tx actually compared (after tx_ids filter)
    compared_tx_count: int       # alias of in_scope_tx_count for clarity
    covered_tx_count: int        # of those, actually computed (== in_scope
    covered: list[str] = Field(
        default_factory=list,
        description="tx_ids actually computed (coverage annotation)",
    )
    missing_tx_ids: list[str] = Field(
        default_factory=list,
        description="requested tx_ids absent from the pinned data version",
    )
    complete: bool
    partial_reason: str | None = None
    truncated: bool = False
    new_hits: list[TxDiff] = Field(default_factory=list)
    lost_hits: list[TxDiff] = Field(default_factory=list)
    changed: list[TxDiff] = Field(default_factory=list)
    new_hits_count: int = 0      # total before per-list truncation
    lost_hits_count: int = 0
    changed_count: int = 0
    unchanged_count: int = 0
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Other API responses
# --------------------------------------------------------------------------- #
class ImportResponse(BaseModel):
    batch_id: str
    imported: bool
    deduped: bool = False
    data_version: int


class PublishResponse(BaseModel):
    version: int
    rule_count: int
    created_at: int


class RollbackResponse(BaseModel):
    version: int
    rolled_back_to: int


class TxResultOut(BaseModel):
    tx_id: str
    score: int
    tags: list[str]
    hit_rule_ids: list[str]
    risk: bool
    rule_version: int
    data_version: int


class AddressRiskOut(BaseModel):
    address: str
    risk: bool
    score: int
    tags: list[str]
    tx_ids: list[str]
    rule_version: int
    data_version: int
