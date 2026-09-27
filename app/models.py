"""Pydantic models used for *responses* (and documentation).

Import payloads are validated manually by app.validation so that every
malformed item can be reported with a stable rejection code instead of
producing opaque pydantic/FastAPI 422 bodies.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ItemStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class BatchStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"


class ItemResult(BaseModel):
    index: int
    tx_hash: str | None = None
    status: ItemStatus
    error: str | None = None
    message: str | None = None


class BatchSubmitResult(BaseModel):
    batch_id: str
    replayed: bool = False
    status: BatchStatus
    data_version: int
    rule_set_version: int
    accepted: int
    duplicate: int
    rejected: int
    items: list[ItemResult]


class BatchStatusResult(BaseModel):
    batch_id: str
    found: bool
    replayed: bool = False
    status: BatchStatus | None = None
    data_version: int | None = None
    rule_set_version: int | None = None
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    items: list[ItemResult] = []


class RuleIn(BaseModel):
    rule_id: str = Field(..., min_length=1, max_length=64)
    kind: str
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    score: int = Field(..., ge=0, le=100)
    tag: str = Field(..., min_length=1, max_length=64)
    effective_at: int | None = None  # ms epoch; default = publish time


class RuleInfo(BaseModel):
    rule_id: str
    version: int
    kind: str
    name: str
    description: str
    params: dict[str, Any]
    score: int
    tag: str
    effective_at: int


class RulePublishResult(BaseModel):
    rule_id: str
    rule_version: int
    rule_set_version: int
    published_at: int
    effective_at: int


class RuleRollbackResult(BaseModel):
    rule_id: str
    from_rule_version: int
    to_rule_version: int
    rule_set_version: int
    rolled_back_at: int
    effective_at: int


class RuleSetInfo(BaseModel):
    rule_set_version: int
    published_at: int
    rules: list[RuleInfo]


class RuleEvidence(BaseModel):
    rule_id: str
    rule_version: int
    rule_set_version: int
    kind: str
    tag: str
    score: int
    detail: str


class RelatedCounterparty(BaseModel):
    address: str
    distance: int
    relation: str | None = None
    cycle: bool = False
    truncated: bool = False
    truncation_reason: str | None = None


class RiskResult(BaseModel):
    tx_hash: str
    found: bool
    data_version: int
    rule_set_version: int
    as_of: int | None = None
    pinned_rule_set_version: int | None = None
    score: int = 0
    labels: list[str] = []
    evidence: list[RuleEvidence] = []
    counterparties: list[RelatedCounterparty] = []
    graph_truncated: bool = False
    graph_truncation_reason: str | None = None
    notes: list[str] = []


class RelationResult(BaseModel):
    accepted: int
    rejected: int
    errors: list[ItemResult]
    data_version: int


class Health(BaseModel):
    status: str
    data_version: int
    rule_set_version: int
    now_ms: int
