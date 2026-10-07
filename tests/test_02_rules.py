"""Regression: rule publish, validation, rollback chain."""

from __future__ import annotations

import pytest

from app.errors import ValidationError
from app.models import ImportBatchIn, RuleIn, RuleType, TxIn

pytestmark = pytest.mark.regression


def _amount(rule_id, threshold, score=10, tags=("big",)):
    return RuleIn(
        rule_id=rule_id,
        rule_type=RuleType.AMOUNT_OVER,
        score=score,
        tags=list(tags),
        params={"threshold": threshold},
    )


def _seed(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)
            ],
        )
    )


def test_publish_creates_monotonic_versions(svc):
    _seed(svc)
    v1 = svc.publish_rules([_amount("r1", 100)])
    v2 = svc.publish_rules([_amount("r2", 50)])
    assert v1.version == 1 and v2.version == 2
    assert svc.current_rule_version().version == 2


def test_empty_publish_rejected(svc):
    with pytest.raises(ValidationError):
        svc.publish_rules([])


def test_duplicate_rule_id_rejected(svc):
    with pytest.raises(ValidationError):
        svc.publish_rules([_amount("same", 100), _amount("same", 200)])


def test_bad_rule_params_rejected(svc):
    # Schema-valid JSON but semantically invalid (fan-out threshold below 1).
    bad_fanout = RuleIn(
        rule_id="fo", rule_type=RuleType.HIGH_FANOUT, score=1,
        tags=[], params={"threshold": 0},
    )
    with pytest.raises(ValidationError):
        svc.publish_rules([bad_fanout])
    bad_blacklist = RuleIn(
        rule_id="bl", rule_type=RuleType.ADDRESS_BLACKLIST, score=1,
        tags=[], params={"addresses": []},
    )
    with pytest.raises(ValidationError):
        svc.publish_rules([bad_blacklist])


def test_rollback_republishes_old_rules_as_new_version(svc):
    _seed(svc)
    svc.publish_rules([_amount("r1", 100, score=10)])
    svc.publish_rules([_amount("r2", 500, score=3)])
    assert svc.query_tx("t1").risk is False  # 200 < 500

    rb = svc.rollback(1)
    assert rb.version == 3 and rb.rolled_back_to == 1
    # Old rules effective again...
    q = svc.query_tx("t1")
    assert q.risk is True and q.score == 10 and q.hit_rule_ids == ["r1"]
    # ...but chain is append-only and old versions remain queryable.
    assert svc.store.get_rule_version(2).rules[0].rule_id == "r2"
