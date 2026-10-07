"""Isolation: dry-run vs concurrent imports/publications/queries."""

from __future__ import annotations

import threading

import pytest

from app.config import Limits
from app.dry_run import run_dry_run
from app.models import (
    DataScope,
    DryRunRequest,
    ImportBatchIn,
    RuleIn,
    RuleType,
    TxIn,
)

pytestmark = pytest.mark.isolation


def _amt(rule_id, threshold, score=10, tags=("big",)):
    return RuleIn(rule_id=rule_id, rule_type=RuleType.AMOUNT_OVER,
                  score=score, tags=list(tags), params={"threshold": threshold})


def _batch(bid, i, amount=200):
    return ImportBatchIn(
        batch_id=bid,
        transactions=[TxIn(tx_id=f"c{i}", **{"from": "A"}, to="B",
                           amount=amount, ts=100 + i)],
    )


def test_dry_run_snapshot_is_stable_while_writes_continue(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="base",
            transactions=[
                TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)
            ],
        )
    )
    svc.publish_rules([_amt("base-rule", 100)])
    req = DryRunRequest(candidate_rules=[_amt("cand", 1)])

    # Force the snapshot to be computed first (d1/v1), then add data+rules.
    snap = svc.load_snapshot(req.scope)
    assert snap.version == 1
    result_holder = {}

    def dry() -> None:
        result_holder["res"] = run_dry_run(
            svc, req, limits=Limits(1000, 1000, 1000, 1000)
        )

    def writes() -> None:
        for i in range(30):
            svc.import_batch(_batch(f"cb{i}", i))
            svc.publish_rules([_amt(f"moving-rule-{i}", 1000, score=1, tags=())])

    t_w = threading.Thread(target=writes)
    t_d = threading.Thread(target=dry)
    t_d.start()
    t_w.start()
    t_w.join()
    t_d.join()

    res = result_holder["res"]
    # Trial is pinned to the snapshot it started from, never the moving target.
    assert res.data_version == 1
    assert res.total_tx_count == 1
    assert res.covered == ["t1"]
    # Official state advanced independently.
    assert svc.store.data_version_count() == 31
    assert svc.current_rule_version().version == 31
    # Published rules are still the real ones (candidate never took effect).
    q = svc.query_tx("t1")
    # Current published rule has threshold 1000 -> t1 (200) is not flagged.
    assert q.hit_rule_ids == []
    assert q.score == 0


def test_concurrent_dry_runs_do_not_interfere(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="base",
            transactions=[
                TxIn(tx_id=f"t{i}", **{"from": "A"}, to="B", amount=200, ts=i)
                for i in range(20)
            ],
        )
    )
    svc.publish_rules([_amt("base", 100)])

    outs: dict[int, set[str]] = {}
    lock = threading.Lock()

    def worker(threshold: int) -> None:
        res = run_dry_run(
            svc,
            DryRunRequest(candidate_rules=[_amt(f"r{threshold}", threshold)]),
            limits=Limits(1000, 1000, 1000, 1000),
        )
        ids = {d.tx_id for d in res.new_hits + res.lost_hits + res.changed}
        with lock:
            outs[threshold] = ids

    threads = [threading.Thread(target=worker, args=(t,))
               for t in (1, 50, 100, 500, 1000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Deterministic, isolated: each threshold yields its own stable diff.
    assert outs[50] == outs[1]  # both match all 200-amount txs
    # Different rule_id with the same threshold => HIT_RULES_CHANGED on all.
    assert len(outs[100]) == 20
    # All baseline hits are lost under a threshold none of the txs reach.
    assert len(outs[1000]) == 20 and outs[1000]
    # An identical candidate (same rule_id/content) yields no diff at all.
    same = run_dry_run(
        svc, DryRunRequest(candidate_rules=[_amt("base", 100)])
    )
    assert {d.tx_id for d in same.new_hits + same.lost_hits + same.changed} == set()
    assert same.unchanged_count == 20
    # Still no publication happened.
    assert svc.current_rule_version().version == 1


def test_queries_during_dry_run_see_only_published_version(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="base",
            transactions=[
                TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)
            ],
        )
    )
    svc.publish_rules([_amt("live", 100, score=10)])
    stop = threading.Event()
    seen: list[tuple[int, tuple]] = []

    def trial() -> None:
        # A candidate that, if it leaked, would give t1 score 77/tag leak.
        run_dry_run(
            svc,
            DryRunRequest(candidate_rules=[_amt("leak", 1, 77, ("leak",))]),
            limits=Limits(1000, 1000, 1000, 1000),
        )

    def reader() -> None:
        while not stop.is_set():
            q = svc.query_tx("t1")
            seen.append((q.score, tuple(q.tags)))

    tr = threading.Thread(target=reader)
    tt = threading.Thread(target=trial)
    tr.start()
    tt.start()
    tt.join()
    stop.set()
    tr.join()

    assert seen, "reader ran at least once"
    assert all(s == (10, ("big",)) for s in seen)


def test_dry_run_with_scoped_tx_filter_uses_full_graph_structure(svc):
    # Even when scoping to one tx, loop detection sees the whole prefix.
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id="a", **{"from": "A"}, to="B", amount=1, ts=1),
                TxIn(tx_id="b", **{"from": "B"}, to="C", amount=1, ts=2),
                TxIn(tx_id="c", **{"from": "C"}, to="A", amount=1, ts=3),
            ],
        )
    )
    svc.publish_rules([RuleIn(rule_id="base", rule_type=RuleType.LOOP_DETECT,
                              score=1, tags=[], params={})])
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[RuleIn(rule_id="cand", rule_type=RuleType.LOOP_DETECT,
                                    score=2, tags=["x"], params={})],
            scope=DataScope(tx_ids=["c"]),
        ),
    )
    # "c" is recognized as a loop because graph built over the whole prefix.
    (d,) = res.changed
    assert d.tx_id == "c" and d.candidate.score == 2
