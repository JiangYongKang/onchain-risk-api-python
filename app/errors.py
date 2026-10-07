"""Stable, distinguishable API error types.

Internal tracebacks must never leak to callers; every failure surfaced by the
service layer is an ApiError carrying a stable machine-readable ``code``.
"""
from __future__ import annotations


class ApiError(Exception):
    """An error that is safe to return to API callers."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


# Stable error codes
BATCH_CONFLICT = "BATCH_CONFLICT"
BATCH_NOT_FOUND = "BATCH_NOT_FOUND"
RULE_VERSION_NOT_FOUND = "RULE_VERSION_NOT_FOUND"
DATA_VERSION_NOT_FOUND = "DATA_VERSION_NOT_FOUND"
INVALID_AS_OF = "INVALID_AS_OF"
DRY_RUN_LIMIT_EXCEEDED = "DRY_RUN_LIMIT_EXCEEDED"
VALIDATION_ERROR = "VALIDATION_ERROR"
