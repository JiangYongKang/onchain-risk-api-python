"""Stable rejection/error codes and the service-level exception.

All rejection reasons are *stable strings* so callers can distinguish them;
raw underlying exceptions are never leaked to API clients.
"""
from __future__ import annotations

# ---- Batch envelope (whole-request) rejections -> HTTP 4xx ---------------
BATCH_ID_MISSING = "BATCH_ID_MISSING"
BATCH_EMPTY = "BATCH_EMPTY"
BATCH_TOO_LARGE = "BATCH_TOO_LARGE"
BATCH_REPLAY_MISMATCH = "BATCH_REPLAY_MISMATCH"

# ---- Per-item rejections ---------------------------------------------------
ITEM_FIELD_MISSING = "ITEM_FIELD_MISSING"
ITEM_NOT_OBJECT = "ITEM_NOT_OBJECT"
TX_HASH_INVALID = "TX_HASH_INVALID"
ADDRESS_INVALID = "ADDRESS_INVALID"
AMOUNT_NOT_NUMBER = "AMOUNT_NOT_NUMBER"
AMOUNT_NOT_POSITIVE = "AMOUNT_NOT_POSITIVE"
AMOUNT_TOO_LARGE = "AMOUNT_TOO_LARGE"
TS_INVALID = "TS_INVALID"
TS_OUT_OF_RANGE = "TS_OUT_OF_RANGE"
ITEM_DUPLICATE_IN_BATCH = "ITEM_DUPLICATE_IN_BATCH"
ITEM_CONFLICT_IN_BATCH = "ITEM_CONFLICT_IN_BATCH"
TX_HASH_CONFLICT = "TX_HASH_CONFLICT"

# ---- Rule rejections -------------------------------------------------------
RULE_INVALID = "RULE_INVALID"
RULE_NOT_FOUND = "RULE_NOT_FOUND"
RULE_VERSION_NOT_FOUND = "RULE_VERSION_NOT_FOUND"

# ---- Risk / generic ---------------------------------------------------------
TX_NOT_FOUND = "TX_NOT_FOUND"
VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
ASOF_INVALID = "ASOF_INVALID"
RELATION_CYCLE = "RELATION_CYCLE"
FANOUT_TOO_LARGE = "FANOUT_TOO_LARGE"
RELATION_BATCH_TOO_LARGE = "RELATION_BATCH_TOO_LARGE"


class ServiceError(Exception):
    """Application error carrying a stable code and human-readable message."""

    def __init__(self, code: str, message: str, *, status_code: int = 422,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}
