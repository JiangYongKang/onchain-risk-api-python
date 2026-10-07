"""HTTP API wiring.

Endpoints
---------
* ``POST /v1/batches``                   import (idempotent by batch_id)
* ``POST /v1/rules/publish``             publish a new rule version
* ``POST /v1/rules/rollback/{version}``  rollback (republish an old version)
* ``GET  /v1/rules/current``             current published rule version
* ``GET  /v1/transactions/{tx_id}``      per-tx risk (current/historical pin)
* ``GET  /v1/addresses/{address}``       aggregated address risk
* ``POST /v1/dry-runs``                  rule trial run + version diff

Domain errors are mapped to stable JSON envelopes; unexpected internal errors
are returned as a generic ``INTERNAL_ERROR`` without leaking internals.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Limits
from app.dry_run import run_dry_run
from app.errors import APIError
from app.models import (
    DataScope,
    DryRunRequest,
    ImportBatchIn,
    PublishRulesRequest,
    RollbackRequest,
)
from app.service import RiskService
from app.store import Store

logger = logging.getLogger("onchain_risk")


def create_app(service: RiskService | None = None) -> FastAPI:
    app = FastAPI(title="onchain-risk-api", version="0.1.0")
    svc = service or RiskService(Store())
    limits = Limits.from_env()
    app.state.service = svc

    # ------------------------------------------------------------------ #
    # Error handlers
    # ------------------------------------------------------------------ #
    @app.exception_handler(APIError)
    async def _handle_api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unknown(_: Request, exc: Exception) -> JSONResponse:
        # Log internally, but never forward internals/tracebacks to callers.
        logger.exception("unexpected error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR",
                               "message": "internal error", "details": {}}},
        )

    # ------------------------------------------------------------------ #
    # Meta / imports / rule lifecycle
    # ------------------------------------------------------------------ #
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/batches")
    def import_batch(payload: ImportBatchIn) -> dict[str, Any]:
        return svc.import_batch(payload).model_dump()

    @app.post("/v1/rules/publish")
    def publish_rules(payload: PublishRulesRequest) -> dict[str, Any]:
        return svc.publish_rules(
            payload.rules, note=payload.note, created_at=payload.created_at
        ).model_dump()

    @app.post("/v1/rules/rollback/{version}")
    def rollback(version: int, payload: RollbackRequest | None = None) -> dict[str, Any]:
        note = payload.note if payload else ""
        return svc.rollback(version, note=note).model_dump()

    @app.get("/v1/rules/current")
    def current_rules() -> dict[str, Any]:
        rs = svc.current_rule_version()
        if rs is None:
            return {"version": 0, "rules": [], "created_at": None}
        return {
            "version": rs.version,
            "created_at": rs.created_at,
            "note": rs.note,
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "rule_type": r.rule_type.value,
                    "score": r.score,
                    "tags": list(r.tags),
                    "params": r.params,
                }
                for r in rs.rules
            ],
        }

    # ------------------------------------------------------------------ #
    # Risk queries
    # ------------------------------------------------------------------ #
    @app.get("/v1/transactions/{tx_id}")
    def query_tx(
        tx_id: str,
        rule_version: int | None = None,
        as_of: int | None = None,
        data_version: int | None = None,
    ) -> dict[str, Any]:
        scope = (
            DataScope(kind="data_version", data_version=data_version)
            if data_version is not None
            else None
        )
        return svc.query_tx(tx_id, scope=scope, rule_version=rule_version,
                            as_of=as_of).model_dump()

    @app.get("/v1/addresses/{address}")
    def query_address(
        address: str,
        rule_version: int | None = None,
        as_of: int | None = None,
        data_version: int | None = None,
    ) -> dict[str, Any]:
        scope = (
            DataScope(kind="data_version", data_version=data_version)
            if data_version is not None
            else None
        )
        return svc.query_address(address, scope=scope, rule_version=rule_version,
                                 as_of=as_of).model_dump()

    # ------------------------------------------------------------------ #
    # Dry run
    # ------------------------------------------------------------------ #
    @app.post("/v1/dry-runs")
    def dry_run(payload: DryRunRequest) -> dict[str, Any]:
        return run_dry_run(svc, payload, limits=limits).model_dump()

    return app
