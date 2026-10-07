"""Regression: concurrent imports / publications / reads stay consistent."""

from __future__ import annotations

import threading

import pytest

from app.errors import ConflictError
from app.models import ImportBatchIn, RuleIn, RuleType, TxIn

pytestmark = pytest.mark.regression


def test_concurrent_distinct_imports_are_all_counted(svc):
    n = 40
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            svc.import_batch(
                ImportBatchIn(
                    batch_id=f"b{i}",
                    transactions=[
                        TxIn(tx_id=f"t{i}", **{"from": "A"}, to="B",
                             amount=100, ts=i)
                    ],
                )
            )
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert svc.store.data_version_count() == n


def test_concurrent_duplicate_import_never_double_applies(svc):
    def payload() -> ImportBatchIn:
        return ImportBatchIn(
            batch_id="same",
            transactions=[TxIn(tx_id="t1", **{"from": "A"}, to="B",
                               amount=10, ts=1)],
        )

    results: list[bool] = []
    conflicts: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            r = svc.import_batch(payload())
            with lock:
                results.append(r.imported)
        except ConflictError as e:
            with lock:
                conflicts.append(e.code)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one worker applies the batch; the rest are idempotent (same
    # content, so conflict list must stay empty).
    assert results.count(True) == 1
    assert conflicts == []
    assert svc.store.data_version_count() == 1


def test_concurrent_publish_and_query_always_sees_complete_version(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)
            ],
        )
    )
    stop = threading.Event()
    bad: list[str] = []

    def publisher() -> None:
        for i in range(1, 31):
            svc.publish_rules(
                [RuleIn(rule_id=f"r{i}", rule_type=RuleType.AMOUNT_OVER,
                        score=i, tags=[f"tag{i}"], params={"threshold": 100})]
            )

    def reader() -> None:
        while not stop.is_set():
            q = svc.query_tx("t1")
            # A reader must see a coherent version: rule ids/tags/score agree.
            if q.risk and not (q.hit_rule_ids and q.tags and q.score > 0):
                bad.append("incoherent read")
                break

    tp = threading.Thread(target=publisher)
    tr = threading.Thread(target=reader)
    tr.start()
    tp.start()
    tp.join()
    stop.set()
    tr.join()

    assert not bad
    assert svc.store.rule_version_count() == 30
