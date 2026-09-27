"""FastAPI wiring.

All business errors are :class:`ServiceError` instances carrying stable
codes; they are rendered uniformly and raw internal exceptions are never
leaked (a generic 500 envelope is returned instead).
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import ServiceError
from .models import Health
from .service import RiskService

logger = logging.getLogger("risk.api")


def create_app(service: RiskService | None = None) -> FastAPI:
    svc = service or RiskService()
    app = FastAPI(
        title="onchain-risk-api",
        version="0.1.0",
        description="Local on-chain transaction risk analysis API "
                    "(batches, address relations, versioned rules).",
    )

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        logger.warning("service_error code=%s %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        # never leak underlying exceptions to clients
        logger.exception("unexpected_error")
        return JSONResponse(
            status_code=500,
            content={"error": "INTERNAL_ERROR",
                     "message": "unexpected internal error", "details": {}})

    @app.get("/health", response_model=Health)
    def health():
        snap = svc.store.snapshot()
        return Health(status="ok", data_version=snap.data_version,
                      rule_set_version=snap.rule_set.version,
                      now_ms=svc.clock.now_ms())

    # -- batches -----------------------------------------------------------
    @app.post("/api/v1/batches")
    async def submit_batch(request: Request):
        return svc.submit_batch(await _json(request))

    @app.get("/api/v1/batches/{batch_id}")
    def get_batch(batch_id: str):
        return svc.get_batch(batch_id)

    # -- relations ---------------------------------------------------------
    @app.post("/api/v1/relations")
    async def import_relations(request: Request):
        return svc.import_relations(await _json(request))

    # -- rules -------------------------------------------------------------
    @app.post("/api/v1/rules")
    async def publish_rule(request: Request):
        return svc.publish_rule(await _json(request))

    @app.post("/api/v1/rules/{rule_id}/rollback")
    async def rollback_rule(rule_id: str, request: Request):
        body = await _json(request)
        target = body.get("target_version") if isinstance(body, dict) else None
        return svc.rollback_rule(rule_id, target)

    @app.get("/api/v1/rulesets")
    def get_ruleset(version: int | None = None):
        return svc.get_ruleset(version)

    # -- risk --------------------------------------------------------------
    @app.get("/api/v1/risk/{tx_hash}")
    def risk(tx_hash: str, rule_set_version: int | None = None,
             as_of_ms: int | None = None):
        return svc.risk(tx_hash, rule_set_version=rule_set_version,
                        as_of_ms=as_of_ms)

    return app


async def _json(request: Request):
    try:
        return await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ServiceError("BAD_REQUEST", "request body must be valid JSON",
                           status_code=400)
