"""FastAPI HTTP layer."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import ApiError
from app.models import (
    DryRunRequest,
    ImportBatchRequest,
    PublishRulesRequest,
    RollbackRequest,
)
from app.service import RiskService

app = FastAPI(title="onchain-risk-api")
service = RiskService()


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Never leak internal errors to callers.
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "internal error"}},
    )


@app.post("/transactions/batches")
def import_batch(req: ImportBatchRequest):
    return service.import_batch(req.batch_id, req.transactions)


@app.post("/rules/publish")
def publish_rules(req: PublishRulesRequest):
    return service.publish_rules(req.rules, req.note)


@app.post("/rules/rollback")
def rollback_rules(req: RollbackRequest):
    return service.rollback_rules(req.version)


@app.get("/rules/versions")
def list_rule_versions():
    return service.list_rule_versions()


@app.get("/risk")
def query_risk(data_version: int | None = None,
               rule_version: int | None = None,
               as_of: float | None = None):
    return service.query_risk(data_version=data_version,
                              rule_version=rule_version, as_of=as_of)


@app.post("/rules/dry-run")
def dry_run(req: DryRunRequest):
    return service.dry_run(req)
