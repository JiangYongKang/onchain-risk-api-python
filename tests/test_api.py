"""HTTP-level tests: routes, stable error payloads, end-to-end dry-run flow."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.service import RiskService


@pytest.fixture()
def client(monkeypatch):
    import app.main as m
    m.service = RiskService()          # fresh state per test
    monkeypatch.setattr(m, "service", m.service)
    return TestClient(app, raise_server_exceptions=False)


def tx(i, amount=1.0, frm="A", to="B"):
    return {"tx_id": f"tx{i}", "from_addr": frm, "to_addr": to,
            "asset": "ETH", "amount": amount, "timestamp": 1000}


def seed(client):
    r = client.post("/transactions/batches",
                    json={"batch_id": "b1",
                          "transactions": [tx(1, 150), tx(2, 10), tx(3, 80)]})
    assert r.status_code == 200
    r = client.post("/rules/publish", json={
        "rules": [{"rule_id": "live-big", "score": 50, "labels": ["big"],
                   "condition": {"min_amount": 100}}], "note": "v1"})
    assert r.status_code == 200


def test_full_flow_import_publish_query_dryrun(client):
    seed(client)
    res = client.get("/risk").json()
    assert {r["tx_id"] for r in res["results"] if r["score"] > 0} == {"tx1"}

    dry = client.post("/rules/dry-run", json={
        "candidate_rules": [{"rule_id": "cand-mid", "score": 40,
                             "labels": ["mid"],
                             "condition": {"min_amount": 50}}]})
    assert dry.status_code == 200
    body = dry.json()
    assert body["baseline_rule_version"] == 1
    assert body["summary"]["added"] == 1      # tx3
    assert body["summary"]["changed"] == 1    # tx1
    assert body["coverage"]["full"] is True
    by_id = {d["tx_id"]: d for d in body["diffs"]}
    assert [h["rule_id"] for h in by_id["tx3"]["candidate"]["hits"]] == ["cand-mid"]
    assert [h["rule_id"] for h in by_id["tx1"]["baseline"]["hits"]] == ["live-big"]

    # dry-run did not publish anything
    assert len(client.get("/rules/versions").json()) == 1


def test_error_payload_shape_is_stable(client):
    seed(client)
    r = client.post("/transactions/batches",
                    json={"batch_id": "b1", "transactions": [tx(9)]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_CONFLICT"

    r = client.get("/risk", params={"rule_version": 99})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "RULE_VERSION_NOT_FOUND"

    r = client.post("/rules/dry-run", json={"candidate_rules": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_internal_errors_are_not_leaked(client, monkeypatch):
    import app.main as m

    def boom(_req):
        raise RuntimeError("secret internals /tmp/secret/path")
    monkeypatch.setattr(m.service, "dry_run", boom)
    r = client.post("/rules/dry-run", json={
        "candidate_rules": [{"rule_id": "c", "score": 1,
                             "condition": {"min_amount": 1}}]})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in str(body)


def test_rollback_via_api(client):
    seed(client)
    client.post("/rules/publish", json={
        "rules": [{"rule_id": "strict", "score": 99,
                   "condition": {"min_amount": 100000}}]})
    r = client.post("/rules/rollback", json={"version": 1})
    assert r.status_code == 200 and r.json()["active"] is True
    res = client.get("/risk").json()
    assert {x["tx_id"] for x in res["results"] if x["score"] > 0} == {"tx1"}


def test_historical_query_via_api(client):
    seed(client)
    import time
    t = time.time()
    time.sleep(0.01)
    client.post("/transactions/batches",
                json={"batch_id": "b2", "transactions": [tx(4, 999)]})
    client.post("/rules/publish", json={
        "rules": [{"rule_id": "strict", "score": 1,
                   "condition": {"min_amount": 100000}}]})
    res = client.get("/risk", params={"as_of": t}).json()
    assert res["data_version"] == 1 and res["rule_version"] == 1
    assert [r["tx_id"] for r in res["results"]] == ["tx1", "tx2", "tx3"]
