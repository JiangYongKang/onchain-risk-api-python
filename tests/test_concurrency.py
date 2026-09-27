"""Concurrent import / rule switch / query consistency."""
import threading

from tests.conftest import tx


def test_same_batch_concurrent_submit_counts_once(client):
    payload = {"batch_id": "b1", "transactions": [tx("0xaaaaaa"), tx("0xbbbbbb")]}
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(client.post("/api/v1/batches", json=payload).json())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted_versions = {r["data_version"] for r in results}
    assert accepted_versions == {1}
    assert sum(1 for r in results if r["replayed"]) == 7
    health = client.get("/health").json()
    assert health["data_version"] == 1


def test_overlapping_batches_concurrent_no_double_count(client, service):
    # 8 threads, each submits a batch sharing tx 0xaaaaaa + one unique tx
    def worker(i):
        h = f"0x{i+2:06x}"
        client.post("/api/v1/batches", json={
            "batch_id": f"b{i}",
            "transactions": [tx("0xaaaaaa"), tx(h)]})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = service.store.snapshot()
    # shared tx counted exactly once + 8 unique txs
    assert len(snap.transactions) == 9
    dup_batches = 0
    for b in snap.batches.values():
        assert b.status.value in ("ACCEPTED", "PARTIAL")
        dup_batches += b.duplicate
    assert dup_batches == 7


def test_concurrent_publishes_are_linearizable(client, service):
    def publish(i):
        client.post("/api/v1/rules", json={
            "rule_id": f"r{i}", "kind": "TX_AMOUNT", "name": f"r{i}",
            "score": 10, "tag": f"T{i}", "params": {"min_amount": 100}})

    threads = [threading.Thread(target=publish, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rs = service.store.ruleset()
    assert rs.version == 10
    assert len(rs.rules) == 10
    # every ruleset version held exactly one complete set of rules
    for v in range(1, 11):
        assert len(service.store.ruleset(v).rules) == v


def test_queries_during_writes_always_observe_complete_version(client, service):
    client.post("/api/v1/batches", json={
        "batch_id": "seed",
        "transactions": [tx("0xaaaaaa", amount=200)]})
    client.post("/api/v1/rules", json={
        "rule_id": "big", "kind": "TX_AMOUNT", "name": "big",
        "score": 40, "tag": "LARGE", "params": {"min_amount": 100}})

    observed = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            r = client.get("/api/v1/risk/0xaaaaaa").json()
            # evidence rule_set_version must equal the response version:
            # a query never mixes rule sets
            if r["evidence"]:
                evs = {e["rule_set_version"] for e in r["evidence"]}
                assert evs == {r["rule_set_version"]}
                observed.append(r["rule_set_version"])

    th = threading.Thread(target=reader)
    th.start()
    for score in range(41, 61):
        client.post("/api/v1/rules", json={
            "rule_id": "big", "kind": "TX_AMOUNT", "name": "big",
            "score": score, "tag": "LARGE",
            "params": {"min_amount": 100}})
    stop.set()
    th.join()
    assert observed, "reader should have observed at least one complete version"
    assert service.store.ruleset().version == 21


def test_concurrent_relation_imports_never_duplicate_edges(client, service):
    def worker(i):
        client.post("/api/v1/relations", json={
            "relations": [
                {"from_addr": "0xaaaaaa", "to_addr": "0xbbbbbb", "relation": "xfer"},
                {"from_addr": f"0x{i+2:06x}", "to_addr": "0xbbbbbb"},
            ]})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = service.store.snapshot()
    assert snap.outgoing["0xaaaaaa"] == frozenset({"0xbbbbbb"})
    assert len(snap.outgoing) == 11
