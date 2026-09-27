"""Duplicate batches, distinguishable rejections and partial success."""
from tests.conftest import tx


def test_batch_basic_acceptance(client):
    r = client.post("/api/v1/batches", json={
        "batch_id": "b1", "transactions": [tx("0xaaaaaa"), tx("0xbbbbbb")]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ACCEPTED"
    assert body["accepted"] == 2 and body["rejected"] == 0
    assert body["data_version"] == 1
    assert body["replayed"] is False


def test_identical_batch_replay_is_idempotent(client):
    payload = {"batch_id": "b1", "transactions": [tx("0xaaaaaa")]}
    r1 = client.post("/api/v1/batches", json=payload).json()
    r2 = client.post("/api/v1/batches", json=payload).json()
    assert r1["data_version"] == 1
    assert r2["replayed"] is True
    assert r2["data_version"] == 1
    # the stored tx is counted exactly once
    assert client.get("/health").json()["data_version"] == 1


def test_overlapping_batches_do_not_double_count(client):
    client.post("/api/v1/batches", json={
        "batch_id": "b1", "transactions": [tx("0xaaaaaa"), tx("0xbbbbbb")]})
    r = client.post("/api/v1/batches", json={
        "batch_id": "b2",
        "transactions": [tx("0xaaaaaa"), tx("0xcccccc")]}).json()
    assert r["status"] == "PARTIAL"
    assert r["accepted"] == 1 and r["duplicate"] == 1
    dup = next(i for i in r["items"] if i["tx_hash"] == "0xaaaaaa")
    assert dup["status"] == "DUPLICATE" and dup["error"] == "TX_HASH_DUPLICATE"
    assert "b1" in dup["message"]


def test_rejection_codes_are_distinguishable(client):
    r = client.post("/api/v1/batches", json={"batch_id": "b1", "transactions": [
        "not-an-object",
        {"from_addr": "0xa1aaaa", "to_addr": "0xb1bbbb", "amount": 1, "ts_ms": 1},
        {"tx_hash": 123, "from_addr": "0xa1aaaa", "to_addr": "0xb1bbbb", "amount": 1, "ts_ms": 1},
        tx("0xbad001", frm="zzz"),
        tx("0xbad002", amount="10"),
        tx("0xbad003", amount=0),
        tx("0xbad004", amount=1e19),
        tx("0xbad005", ts="1759000000000"),
        tx("0xbad006", ts=1),
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "REJECTED" and body["accepted"] == 0
    codes = [i["error"] for i in body["items"]]
    assert codes == [
        "ITEM_NOT_OBJECT", "ITEM_FIELD_MISSING", "TX_HASH_INVALID",
        "ADDRESS_INVALID", "AMOUNT_NOT_NUMBER", "AMOUNT_NOT_POSITIVE",
        "AMOUNT_TOO_LARGE", "TS_INVALID", "TS_OUT_OF_RANGE",
    ]


def test_in_batch_duplicate_and_contradiction(client):
    r = client.post("/api/v1/batches", json={"batch_id": "b1", "transactions": [
        tx("0xaaaaaa", amount=10),
        tx("0xaaaaaa", amount=10),                       # duplicate
        tx("0xaaaaaa", frm="0x999999", amount=10),       # contradiction
    ]}).json()
    statuses = [(i["status"], i["error"]) for i in r["items"]]
    assert statuses[0][0] == "ACCEPTED"
    assert statuses[1] == ("DUPLICATE", "ITEM_DUPLICATE_IN_BATCH")
    assert statuses[2] == ("REJECTED", "ITEM_CONFLICT_IN_BATCH")
    assert r["accepted"] == 1


def test_stored_tx_conflict_is_rejected(client):
    client.post("/api/v1/batches", json={
        "batch_id": "b1", "transactions": [tx("0xaaaaaa", amount=10)]})
    r = client.post("/api/v1/batches", json={
        "batch_id": "b2", "transactions": [tx("0xaaaaaa", amount=999)]}).json()
    assert r["items"][0]["error"] == "TX_HASH_CONFLICT"
    assert r["accepted"] == 0


def test_replay_with_different_payload_is_409(client):
    client.post("/api/v1/batches", json={
        "batch_id": "b1", "transactions": [tx("0xaaaaaa")]})
    r = client.post("/api/v1/batches", json={
        "batch_id": "b1", "transactions": [tx("0xbbbbbb")]})
    assert r.status_code == 409
    assert r.json()["error"] == "BATCH_REPLAY_MISMATCH"


def test_batch_envelope_errors(client):
    assert client.post("/api/v1/batches", json={}).status_code == 422
    assert client.post("/api/v1/batches", json={"batch_id": "x"}).status_code == 422
    assert client.post("/api/v1/batches",
                       json={"batch_id": "x", "transactions": []}).status_code == 422
    r = client.post("/api/v1/batches", json={
        "batch_id": "big",
        "transactions": [tx(f"0x{i:06x}") for i in range(10_001)]})
    assert r.status_code == 422 and r.json()["error"] == "BATCH_TOO_LARGE"


def test_batch_status_query(client):
    client.post("/api/v1/batches", json={
        "batch_id": "b1",
        "transactions": [tx("0xaaaaaa"), tx("0xbad001", amount=-1)]})
    r = client.get("/api/v1/batches/b1").json()
    assert r["found"] is True and r["status"] == "PARTIAL"
    assert r["accepted"] == 1 and r["rejected"] == 1
    assert client.get("/api/v1/batches/nope").json()["found"] is False


def test_addresses_are_case_normalized(client):
    client.post("/api/v1/batches", json={
        "batch_id": "b1",
        "transactions": [tx("0xAAAAAA", frm="0xA1AAAA", to="0xB1BBBB")]})
    assert client.get("/api/v1/risk/0xaaaaaa").json()["found"] is True
