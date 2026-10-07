"""Stable, distinguishable error codes for the risk API."""

from __future__ import annotations


class APIError(Exception):
    """Base error carrying a stable code and HTTP-ish status code."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500

    def __init__(self, message: str = "", *, details: dict | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}


class NotFoundError(APIError):
    code = "NOT_FOUND"
    status_code = 404


class ConflictError(APIError):
    code = "CONFLICT"
    status_code = 409


class ValidationError(APIError):
    code = "VALIDATION_ERROR"
    status_code = 422


class LimitExceededError(APIError):
    """Capacity guard: the request (or remaining budget) is over the limit."""

    code = "LIMIT_EXCEEDED"
    status_code = 413


class InternalError(APIError):
    code = "INTERNAL_ERROR"
    status_code = 500
