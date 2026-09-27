"""Cycles, huge fan-out, huge batches and error containment."""
from app import limits
from tests.conftest import tx


def test_relation_cycle_is_detected_not_infinite(client):
    r = client.post("/api/v1/relations", json={"relations": [
        {"from_addr": "0xaaaaaa", "to_addr": "0xbbbbbb"},
        {"from_addr": "0xbbbbbb", "to_addr": "0xcccccc"},
        {"from_addr": "0xcccccc", "to_addr": "0xaaaaaa"},
    ]})
    assert r.status_code == 200
    client.post("/api/v1/batches", json={
        "batch_id": "b",
        "transactions": [tx("0x770001", frm="0xaaaaaa", to="0x111111")]})
    risk = client.get("/api/v1/risk/0x770001").json()
    notes = " ".join(risk["notes"])
    assert "cycle" in notes
    # the a-b-c-a cycle members (other than the queried start) are reported
    assert {"0xbbbbbb", "0xcccccc"} <= set(notes.replace(",", " ").split())
    assert all(c["cycle"] for c in risk["counterparties"])
    # no infinite traversal: counterparties are bounded
    assert {c["address"] for c in risk["counterparties"]} == {
        "0xbbbbbb", "0xcccccc"}


def test_fanout_cap_returns_marked_partial_result(client, monkeypatch):
    monkeypatch.setattr(limits, "MAX_RELATION_BATCH", 20_000)
    n = limits.MAX_FANOUT + 300
    edges = [{"from_addr": "0xaaaaaa", "to_addr": f"0x{i+2:06x}"}
             for i in range(n)]
    assert client.post("/api/v1/relations",
                       json={"relations": edges}).status_code == 200
    client.post("/api/v1/batches", json={
        "batch_id": "b",
        "transactions": [tx("0x770001", frm="0xaaaaaa", to="0x999999")]})
    risk = client.get("/api/v1/risk/0x770001").json()
    assert risk["graph_truncated"] is True
    assert risk["graph_truncation_reason"] == "FANOUT_TOO_LARGE"
    # explicit, bounded partial result rather than silent truncation
    direct = [c for c in risk["counterparties"] if c["distance"] == 1]
    assert len(direct) == limits.MAX_FANOUT
    assert all(c["truncated"] for c in risk["counterparties"])
    assert any("partial" in n for n in risk["notes"])


def test_node_cap_marks_truncation(client, monkeypatch):
    # A long chain: fan-out stays 1 so the node cap binds before fan-out.
    monkeypatch.setattr(limits, "MAX_RELATION_BATCH", 20_000)
    monkeypatch.setattr(limits, "MAX_GRAPH_DEPTH", 10_000)
    total = limits.MAX_GRAPH_NODES + 50
    edges = [{"from_addr": f"0x{i:06x}", "to_addr": f"0x{i+1:06x}"}
             for i in range(total)]
    client.post("/api/v1/relations", json={"relations": edges})
    client.post("/api/v1/batches", json={
        "batch_id": "b",
        "transactions": [tx("0x770001", frm="0x000000", to="0x999999")]})
    risk = client.get("/api/v1/risk/0x770001").json()
    assert risk["graph_truncated"] is True
    assert "GRAPH_NODE_CAP" in risk["graph_truncation_reason"]
    assert len(risk["counterparties"]) == limits.MAX_GRAPH_NODES


def test_oversized_transaction_batch_is_rejected_not_truncated(client):
    r = client.post("/api/v1/batches", json={
        "batch_id": "huge",
        "transactions": [tx(f"0x{i+2:06x}")
                         for i in range(limits.MAX_BATCH_ITEMS + 1)]})
    assert r.status_code == 422
    assert r.json()["error"] == "BATCH_TOO_LARGE"
    # nothing from the rejected batch landed
    assert client.get("/api/v1/batches/huge").json()["found"] is False


def test_oversized_relation_batch_is_rejected(client):
    r = client.post("/api/v1/relations", json={
        "relations": [{"from_addr": "0xaaaaaa", "to_addr": f"0x{i+2:06x}"}
                      for i in range(limits.MAX_RELATION_BATCH + 1)]})
    assert r.status_code == 422
    assert r.json()["error"] == "RELATION_BATCH_TOO_LARGE"


def test_internal_errors_are_not_leaked(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db password=secret")

    # force an unexpected failure inside the service and check the envelope
    monkeypatch.setattr(
        "app.service.validate_transactions", boom)
    r = client.post("/api/v1/batches", json={
        "batch_id": "b", "transactions": [tx("0xaaaaaa")]})
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "INTERNAL_ERROR"
    assert "secret" not in str(body)


def test_fanout_rule_uses_transaction_counterparties(client):
    base = 1_759_000_000_000 - 60_000
    txs = [tx(f"0x{i+10:06x}", frm="0xfa7001", to=f"0x{i+100:06x}",
              amount=1, ts=base + i) for i in range(3)]
    client.post("/api/v1/batches", json={"batch_id": "b", "transactions": txs})
    client.post("/api/v1/rules", json={
        "rule_id": "fan", "kind": "ADDRESS_FANOUT", "name": "fan",
        "score": 70, "tag": "FANOUT",
        "params": {"direction": "out", "min_count": 3,
                   "within_ms": 86_400_000}})
    r = client.get(f"/api/v1/risk/0x{12:06x}").json()
    assert r["score"] == 70 and r["labels"] == ["FANOUT"]
    assert "3 distinct out counterparties" in r["evidence"][0]["detail"]
