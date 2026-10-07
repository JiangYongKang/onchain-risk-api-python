"""Regression: loop/high-fanout detection and historical point reproduction."""

from __future__ import annotations

import pytest

from app.models import (
    DataScope,
    EdgeIn,
    ImportBatchIn,
    RuleIn,
    RuleType,
    TxIn,
)

pytestmark = pytest.mark.regression


def _tx(tid, frm, to, amount=1, ts=0):
    return TxIn(tx_id=tid, **{"from": frm}, to=to, amount=amount, ts=ts)


def test_loop_detected_when_tx_closes_cycle(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                _tx("a", "A", "B", ts=1),
                _tx("b", "B", "C", ts=2),
                _tx("c", "C", "A", ts=3),  # closes A->B->C->A
            ],
        )
    )
    svc.publish_rules(
        [RuleIn(rule_id="loop", rule_type=RuleType.LOOP_DETECT, score=9,
                tags=["cycle"], params={})]
    )
    assert svc.query_tx("a").risk is False
    assert svc.query_tx("b").risk is False
    q = svc.query_tx("c")
    assert q.risk is True and q.hit_rule_ids == ["loop"]


def test_self_loop_and_explicit_edge_participate(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[_tx("a", "A", "A", ts=1), _tx("b", "A", "B", ts=2)],
            edges=[EdgeIn(**{"from": "B"}, to="A")],  # B->A then A->B closes loop
        )
    )
    svc.publish_rules(
        [RuleIn(rule_id="loop", rule_type=RuleType.LOOP_DETECT, score=1,
                tags=["cycle"], params={})]
    )
    # Self-loop is not counted as a graph cycle, but A->B after explicit B->A is.
    assert svc.query_tx("a").risk is False
    assert svc.query_tx("b").risk is True


def test_high_fanout_uses_distinct_out_neighbours(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                _tx("a", "A", "B", ts=1),
                _tx("b", "A", "C", ts=2),
                _tx("c", "A", "C", ts=3),  # duplicate neighbour, not counted
            ],
        )
    )
    svc.publish_rules(
        [RuleIn(rule_id="fan", rule_type=RuleType.HIGH_FANOUT, score=5,
                tags=["fanout"], params={"threshold": 2})]
    )
    assert svc.query_tx("a").risk is False
    assert svc.query_tx("b").risk is True
    assert svc.query_tx("c").risk is True  # still 2 distinct neighbours


def test_historical_point_uses_data_and_rules_of_that_instant(svc):
    # t=1000 import; v1 published at 1100; second import t=1200; v2 at 1300.
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1", imported_at=1000,
            transactions=[_tx("t1", "A", "B", amount=200, ts=1)],
        )
    )
    svc.publish_rules(
        [RuleIn(rule_id="v1", rule_type=RuleType.AMOUNT_OVER, score=10,
                tags=["big"], params={"threshold": 100})],
        created_at=1100,
    )
    svc.import_batch(
        ImportBatchIn(
            batch_id="b2", imported_at=1200,
            transactions=[_tx("t2", "A", "C", amount=300, ts=2)],
        )
    )
    svc.publish_rules(
        [RuleIn(rule_id="v2", rule_type=RuleType.AMOUNT_OVER, score=3,
                tags=["huge"], params={"threshold": 500})],
        created_at=1300,
    )

    # At 1050: no rules yet.
    early = svc.query_tx("t1", as_of=1050)
    assert early.rule_version == 0 and early.risk is False

    # At 1150: v1 + only b1.
    mid = svc.query_tx("t1", as_of=1150)
    assert mid.rule_version == 1 and mid.data_version == 1 and mid.score == 10

    # t2 must be unknown at 1150 (imported later) even though it exists now.
    from app.errors import NotFoundError
    with pytest.raises(NotFoundError):
        svc.query_tx("t2", as_of=1150)

    # At 1400: v2 (500 threshold) => neither tx risky.
    now_q = svc.query_tx("t2", as_of=1400)
    assert now_q.rule_version == 2 and now_q.data_version == 2
    assert now_q.risk is False


def test_explicit_version_pins_are_reproducible(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[_tx("t1", "A", "B", amount=200, ts=1)],
        )
    )
    svc.publish_rules(
        [RuleIn(rule_id="v1", rule_type=RuleType.AMOUNT_OVER, score=10,
                tags=["big"], params={"threshold": 100})]
    )
    svc.publish_rules(
        [RuleIn(rule_id="v2", rule_type=RuleType.AMOUNT_OVER, score=1,
                tags=["tiny"], params={"threshold": 10})]
    )
    current = svc.query_tx("t1")
    pinned = svc.query_tx("t1", rule_version=1)
    assert current.hit_rule_ids == ["v2"] and pinned.hit_rule_ids == ["v1"]
    assert pinned.score == 10 and pinned.tags == ["big"]


def test_data_version_pin_replays_prefix(svc):
    svc.import_batch(
        ImportBatchIn(batch_id="b1",
                      transactions=[_tx("t1", "A", "B", amount=200, ts=1)])
    )
    svc.import_batch(
        ImportBatchIn(batch_id="b2",
                      transactions=[_tx("t2", "B", "C", amount=200, ts=2)])
    )
    svc.publish_rules(
        [RuleIn(rule_id="r", rule_type=RuleType.AMOUNT_OVER, score=10,
                tags=["big"], params={"threshold": 100})]
    )
    scope = DataScope(kind="data_version", data_version=1)
    assert svc.query_tx("t1", scope=scope).risk is True
    from app.errors import NotFoundError
    with pytest.raises(NotFoundError):
        svc.query_tx("t2", scope=scope)
