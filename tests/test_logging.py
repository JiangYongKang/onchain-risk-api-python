"""Logs must carry batch id, versions and per-rule decision basis."""
import json
import logging

from tests.conftest import tx


def test_batch_and_risk_logs_contain_versions_and_evidence(client, caplog):
    caplog.set_level(logging.INFO, logger="risk.api")
    client.post("/api/v1/batches", json={
        "batch_id": "log-batch",
        "transactions": [tx("0xaaaaaa", amount=200),
                         tx("0xbad001", amount=-1)]})
    client.post("/api/v1/rules", json={
        "rule_id": "big", "kind": "TX_AMOUNT", "name": "big",
        "score": 40, "tag": "LARGE", "params": {"min_amount": 100}})
    client.get("/api/v1/risk/0xaaaaaa")

    events = []
    for rec in caplog.records:
        try:
            events.append(json.loads(rec.getMessage()))
        except (ValueError, TypeError):
            pass
    by_event = {e["event"]: e for e in events}

    submit = by_event["batch_submit"]
    assert submit["batch_id"] == "log-batch"
    assert submit["data_version"] == 1
    assert submit["rule_set_version"] == 0
    assert submit["status"] == "PARTIAL"
    assert submit["rejections"] == {"AMOUNT_NOT_POSITIVE": 1}

    risk = by_event["risk_query"]
    assert risk["tx_hash"] == "0xaaaaaa"
    assert risk["rule_set_version"] == 1
    assert risk["score"] == 40 and risk["labels"] == ["LARGE"]
    assert risk["hits"][0]["rule_id"] == "big"
    assert risk["hits"][0]["rule_version"] == 1
