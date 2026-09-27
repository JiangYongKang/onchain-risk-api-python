"""Boundary cases: all-rejected batches, ruleset listing, bad as_of."""
from tests.conftest import tx


def test_all_rejected_batch_does_not_block_hash_later(client):
    r = client.post("/api/v1/batches", json={
        "batch_id": "bad",
        "transactions": [tx("0xaaaaaa", amount=-1)]})
    assert r.json()["status"] == "REJECTED"
    # hash was never stored, so a later valid batch with it is accepted
    r2 = client.post("/api/v1/batches", json={
        "batch_id": "good", "transactions": [tx("0xaaaaaa", amount=10)]})
    assert r2.json()["status"] == "ACCEPTED"


def test_ruleset_listing_and_missing_version(client):
    client.post("/api/v1/rules", json={
        "rule_id": "big", "kind": "TX_AMOUNT", "name": "big",
        "score": 40, "tag": "L", "params": {"min_amount": 1}})
    rs = client.get("/api/v1/rulesets").json()
    assert rs["rule_set_version"] == 1 and len(rs["rules"]) == 1
    v0 = client.get("/api/v1/rulesets?version=0").json()
    assert v0["rules"] == []
    missing = client.get("/api/v1/rulesets?version=99")
    assert missing.status_code == 422
    assert missing.json()["error"] == "VERSION_NOT_FOUND"


def test_bad_as_of_and_missing_tx(client):
    r = client.get("/api/v1/risk/0xaaaaaa?as_of_ms=0")
    assert r.status_code == 422 and r.json()["error"] == "ASOF_INVALID"
    r2 = client.get("/api/v1/risk/0xaaaaaa")
    assert r2.status_code == 200 and r2.json()["found"] is False


def test_invalid_json_body_is_400(client):
    r = client.post("/api/v1/batches", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "BAD_REQUEST"


def test_self_transfer_is_allowed_but_not_special_risk(client):
    client.post("/api/v1/batches", json={
        "batch_id": "b",
        "transactions": [tx("0xaaaaaa", frm="0xabcdef", to="0xabcdef", amount=5)]})
    r = client.get("/api/v1/risk/0xaaaaaa").json()
    assert r["found"] is True and r["score"] == 0


def test_relation_batch_rejects_bad_rows_and_idempotent_edges(client):
    r = client.post("/api/v1/relations", json={"relations": [
        {"from_addr": "0xaaaaaa", "to_addr": "0xbbbbbb"},
        {"from_addr": "0xaaaaaa", "to_addr": "0xbbbbbb"},   # duplicate edge
        {"from_addr": "bad", "to_addr": "0xbbbbbb"},         # bad row
    ]}).json()
    assert r["accepted"] == 1 and r["rejected"] == 1
