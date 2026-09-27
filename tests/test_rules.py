"""Rule publishing, version pinning, rollback and historical replay."""
from tests.conftest import tx


def _publish(client, rid, score, tag, kind="TX_AMOUNT", params=None):
    return client.post("/api/v1/rules", json={
        "rule_id": rid, "kind": kind, "name": rid, "score": score, "tag": tag,
        "params": params or {"min_amount": 100},
    }).json()


def _import_one(client, h, amount=200, clock=None):
    return client.post("/api/v1/batches", json={
        "batch_id": f"batch-{h}",
        "transactions": [tx(h, amount=amount)]})


def test_publish_and_evidence_versions(client):
    _import_one(client, "0xaaaaaa")
    p = _publish(client, "big", 40, "LARGE")
    assert p["rule_version"] == 1 and p["rule_set_version"] == 1
    r = client.get("/api/v1/risk/0xaaaaaa").json()
    assert r["score"] == 40 and r["labels"] == ["LARGE"]
    ev = r["evidence"][0]
    assert ev["rule_id"] == "big" and ev["rule_version"] == 1
    assert ev["rule_set_version"] == 1 and ev["detail"]


def test_multiple_rules_each_have_own_evidence(client):
    _import_one(client, "0xaaaaaa", amount=200)
    _publish(client, "big", 30, "LARGE")
    _publish(client, "watch", 25, "WATCH", kind="WATCHED_ADDRESS",
             params={"addresses": ["0xa1aaaa"], "match": "from"})
    r = client.get("/api/v1/risk/0xaaaaaa").json()
    assert r["score"] == 55
    assert {e["rule_id"] for e in r["evidence"]} == {"big", "watch"}
    # every evidence item references the same rule-set version (no mixing)
    assert len({e["rule_set_version"] for e in r["evidence"]}) == 1


def test_score_is_capped_at_100(client):
    _import_one(client, "0xaaaaaa")
    _publish(client, "r1", 60, "A")
    _publish(client, "r2", 60, "B")
    r = client.get("/api/v1/risk/0xaaaaaa").json()
    assert r["score"] == 100


def test_pinned_old_version_reproduces_old_conclusion(client):
    _import_one(client, "0xaaaaaa")
    _publish(client, "big", 40, "LARGE")
    client.post("/api/v1/rules", json={
        "rule_id": "big", "kind": "TX_AMOUNT", "name": "big",
        "score": 80, "tag": "LARGE", "params": {"min_amount": 100}})
    assert client.get("/api/v1/risk/0xaaaaaa").json()["score"] == 80
    old = client.get("/api/v1/risk/0xaaaaaa?rule_set_version=1").json()
    assert old["score"] == 40
    assert old["evidence"][0]["rule_version"] == 1
    assert old["pinned_rule_set_version"] == 1


def test_rollback_republishes_old_definition_as_new_version(client):
    _import_one(client, "0xaaaaaa")
    _publish(client, "big", 40, "LARGE")
    client.post("/api/v1/rules", json={
        "rule_id": "big", "kind": "TX_AMOUNT", "name": "big",
        "score": 80, "tag": "LARGE", "params": {"min_amount": 100}})
    rb = client.post("/api/v1/rules/big/rollback",
                     json={"target_version": 1}).json()
    assert rb["from_rule_version"] == 2 and rb["to_rule_version"] == 3
    cur = client.get("/api/v1/risk/0xaaaaaa").json()
    assert cur["score"] == 40 and cur["evidence"][0]["rule_version"] == 3
    # historical versions remain intact
    assert client.get("/api/v1/risk/0xaaaaaa?rule_set_version=2")\
        .json()["score"] == 80
    assert client.get("/api/v1/risk/0xaaaaaa?rule_set_version=1")\
        .json()["score"] == 40


def test_rollback_unknown_version_is_stable_error(client):
    _publish(client, "big", 40, "LARGE")
    r = client.post("/api/v1/rules/big/rollback", json={"target_version": 9})
    assert r.status_code == 422
    assert r.json()["error"] == "RULE_VERSION_NOT_FOUND"


def test_as_of_replays_ruleset_at_historical_time(client, clock, service):
    _import_one(client, "0xaaaaaa")
    _publish(client, "big", 40, "LARGE")
    t1 = clock.now_ms()
    clock.advance(10_000)
    client.post("/api/v1/rules", json={
        "rule_id": "big", "kind": "TX_AMOUNT", "name": "big",
        "score": 80, "tag": "LARGE", "params": {"min_amount": 100}})
    t2 = clock.now_ms()
    current = client.get("/api/v1/risk/0xaaaaaa").json()
    historical = client.get(f"/api/v1/risk/0xaaaaaa?as_of_ms={t1}").json()
    assert current["score"] == 80 and historical["score"] == 40
    assert historical["rule_set_version"] == 1
    # as-of after the second publish equals current
    after = client.get(f"/api/v1/risk/0xaaaaaa?as_of_ms={t2}").json()
    assert after["score"] == 80


def test_as_of_hides_transactions_committed_later(client, clock):
    _publish(client, "big", 40, "LARGE")
    t_before = clock.now_ms()
    clock.advance(5_000)
    _import_one(client, "0xaaaaaa")
    r = client.get(f"/api/v1/risk/0xaaaaaa?as_of_ms={t_before}").json()
    assert r["found"] is False


def test_time_window_rule_uses_tx_window_and_sender(client):
    base = 1_759_000_000_000 - 60_000  # safely in the past vs import clock
    txs = [
        tx("0xaaaaaa", frm="0x5e7de1", to="0xc00001", amount=10, ts=base),
        tx("0xbbbbbb", frm="0x5e7de1", to="0xc00002", amount=10, ts=base + 1_000),
        tx("0xcccccc", frm="0x5e7de1", to="0xc00003", amount=10, ts=base + 2_000),
        tx("0xdddddd", frm="0x074e12", to="0xc00004", amount=10, ts=base + 2_000),
    ]
    client.post("/api/v1/batches", json={"batch_id": "b", "transactions": txs})
    client.post("/api/v1/rules", json={
        "rule_id": "burst", "kind": "TIME_WINDOW", "name": "burst",
        "score": 50, "tag": "BURST",
        "params": {"window_ms": 5_000, "max_count": 2}})
    r = client.get("/api/v1/risk/0xcccccc").json()
    assert r["score"] == 50 and r["labels"] == ["BURST"]
    assert "0x5e7de1" in r["evidence"][0]["detail"]
    # other sender only has 1 tx in window -> no hit
    assert client.get("/api/v1/risk/0xdddddd").json()["score"] == 0


def test_invalid_rule_payloads_are_rejected_with_stable_code(client):
    r = client.post("/api/v1/rules", json={
        "rule_id": "x", "kind": "NOPE", "name": "x",
        "score": 50, "tag": "T", "params": {}})
    assert r.status_code == 422 and r.json()["error"] == "RULE_INVALID"
