"""HTTP contract: endpoints, error envelopes, dry-run over HTTP."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.dryrun


def _tx(tid, frm="A", to="B", amount=200, ts=1):
    return {"tx_id": tid, "from": frm, "to": to, "amount": amount, "ts": ts}


def _amt(rule_id, threshold, score=10, tags=("big",)):
    return {"rule_id": rule_id, "rule_type": "amount_over", "score": score,
            "tags": list(tags), "params": {"threshold": threshold}}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_import_and_query_flow(client):
    r = client.post("/v1/batches", json={"batch_id": "b1", "transactions": [_tx("t1")]})
    assert r.status_code == 200 and r.json()["data_version"] == 1
    client.post("/v1/rules/publish", json={"rules": [_amt("amt", 100)]})
    q = client.get("/v1/transactions/t1")
    assert q.status_code == 200
    body = q.json()
    assert body["score"] == 10 and body["risk"] is True
    assert body["rule_version"] == 1 and body["data_version"] == 1


def test_duplicate_batch_http_is_idempotent(client):
    body = {"batch_id": "b1", "transactions": [_tx("t1")]}
    assert client.post("/v1/batches", json=body).json()["imported"] is True
    r2 = client.post("/v1/batches", json=body)
    assert r2.json()["imported"] is False and r2.json()["deduped"] is True


def test_conflict_returns_stable_envelope(client):
    client.post("/v1/batches",
                json={"batch_id": "b1", "transactions": [_tx("t1", amount=1)]})
    r = client.post("/v1/batches",
                    json={"batch_id": "b1", "transactions": [_tx("t1", amount=2)]})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "CONFLICT" and "message" in err and "details" in err


def test_not_found_envelope(client):
    r = client.get("/v1/transactions/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_dry_run_http_diff_and_no_side_effect(client):
    client.post("/v1/batches",
                json={"batch_id": "b1", "transactions": [
                    _tx("t1", amount=200), _tx("t2", amount=60)]})
    client.post("/v1/rules/publish", json={"rules": [_amt("amt", 100)]})
    r = client.post("/v1/dry-runs", json={"candidate_rules": [_amt("m", 50, 5, ("mid",))]})
    assert r.status_code == 200
    body = r.json()
    assert body["baseline_rule_version"] == 1
    new_ids = [d["tx_id"] for d in body["new_hits"]]
    assert new_ids == ["t2"]
    # Side-effect check: current rules unchanged.
    cur = client.get("/v1/rules/current").json()
    assert [x["rule_id"] for x in cur["rules"]] == ["amt"]
    # Official query still baseline.
    assert client.get("/v1/transactions/t2").json()["risk"] is False


def test_dry_run_http_limit_returns_distinct_413(monkeypatch, client):
    # Fresh app with a 1-candidate-rule cap; running app is untouched.
    monkeypatch.setenv("RISK_MAX_CANDIDATE_RULES", "1")
    from app.api import create_app

    c = type(client)(create_app(service=client.app.state.service))
    r = c.post("/v1/dry-runs",
               json={"candidate_rules": [_amt("a", 1), _amt("b", 2)]})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "DRYRUN_RULES_LIMIT_EXCEEDED"


def test_dry_run_http_scope_limit_returns_distinct_413(monkeypatch, client):
    client.post("/v1/batches", json={
        "batch_id": "big",
        "transactions": [_tx(f"t{i}", amount=1, ts=i) for i in range(5)],
    })
    monkeypatch.setenv("RISK_MAX_TX_SCANNED", "3")
    monkeypatch.setenv("RISK_MAX_CANDIDATE_RULES", "100")
    monkeypatch.setenv("RISK_MAX_TX_REPORTED", "100")
    from app.api import create_app

    c = type(client)(create_app(service=client.app.state.service))
    r = c.post("/v1/dry-runs", json={"candidate_rules": [_amt("m", 1)]})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "DRYRUN_SCOPE_TOO_LARGE"


def test_dry_run_http_truncation_annotated(monkeypatch, client):
    client.post("/v1/batches", json={
        "batch_id": "big",
        "transactions": [_tx(f"t{i}", amount=1, ts=i) for i in range(5)],
    })
    monkeypatch.setenv("RISK_MAX_TX_SCANNED", "100")
    monkeypatch.setenv("RISK_MAX_TX_REPORTED", "2")
    monkeypatch.setenv("RISK_MAX_CANDIDATE_RULES", "100")
    monkeypatch.setenv("RISK_MAX_DIFF_ENTRIES", "100")
    from app.api import create_app

    c = type(client)(create_app(service=client.app.state.service))
    r = c.post("/v1/dry-runs", json={"candidate_rules": [_amt("m", 1)]})
    body = r.json()
    assert r.status_code == 200
    assert body["truncated"] is True and body["complete"] is False
    assert body["covered_tx_count"] == 2 and body["in_scope_tx_count"] == 5
    assert body["covered"] == ["t0", "t1"]


def test_unknown_internal_error_is_generic_envelope(client):
    # A TestClient that surfaces the app's 500 response instead of re-raising.
    from fastapi.testclient import TestClient

    c = TestClient(client.app, raise_server_exceptions=False)

    def boom(payload):  # noqa: ANN001
        raise RuntimeError("super secret internal detail")

    client.app.state.service.import_batch = boom  # type: ignore[method-assign]
    r = c.post("/v1/batches",
               json={"batch_id": "b1", "transactions": [_tx("t1")]})
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "INTERNAL_ERROR"
    assert "secret" not in str(err)
