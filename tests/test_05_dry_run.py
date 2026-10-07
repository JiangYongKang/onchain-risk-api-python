"""Dry-run basics: no side effects, candidate views, post-publish equality."""

from __future__ import annotations

import pytest

from app.dry_run import run_dry_run
from app.errors import ValidationError
from app.models import (
    BaselineSpec,
    BaselineSpecKind,
    DataScope,
    DiffKind,
    DryRunRequest,
    ImportBatchIn,
    RuleIn,
    RuleType,
    TxIn,
)

pytestmark = pytest.mark.dryrun


def _amt(rule_id, threshold, score=10, tags=()):
    return RuleIn(rule_id=rule_id, rule_type=RuleType.AMOUNT_OVER,
                  score=score, tags=list(tags), params={"threshold": threshold})


def _loop(rule_id="loop", score=7, tags=("cycle",)):
    return RuleIn(rule_id=rule_id, rule_type=RuleType.LOOP_DETECT,
                  score=score, tags=list(tags), params={})


def _seed(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1),
                TxIn(tx_id="t2", **{"from": "B"}, to="C", amount=60, ts=2),
                TxIn(tx_id="t3", **{"from": "C"}, to="A", amount=30, ts=3),
            ],
        )
    )
    svc.publish_rules([_amt("amt", 100, 10, ("big",))])


def test_dry_run_does_not_change_published_rules(svc):
    _seed(svc)
    before = svc.current_rule_version().version
    run_dry_run(svc, DryRunRequest(candidate_rules=[_amt("x", 1, 99, ("z",))]))
    after = svc.current_rule_version().version
    assert before == after == 1
    # Official query still reflects published rule, not the candidate.
    q = svc.query_tx("t2")
    assert q.risk is False and q.score == 0
    # History/official conclusions are untouched.
    assert svc.query_tx("t1").hit_rule_ids == ["amt"]


def test_candidate_view_reports_hits_score_and_tags(svc):
    _seed(svc)
    res = run_dry_run(
        svc,
        DryRunRequest(candidate_rules=[_amt("m", 50, 5, ("mid",)), _loop()]),
    )
    by_tx = {d.tx_id: d for d in res.new_hits + res.lost_hits + res.changed}
    # t2 newly hits the mid-threshold rule.
    assert by_tx["t2"].kind is DiffKind.NEW_HIT
    assert by_tx["t2"].candidate.score == 5
    assert by_tx["t2"].candidate.tags == ["mid"]
    assert by_tx["t2"].candidate.hit_rule_ids == ["m"]
    assert by_tx["t2"].baseline is not None and by_tx["t2"].baseline.risk is False
    # t3 newly hits loop rule.
    assert by_tx["t3"].candidate.hit_rule_ids == ["loop"]
    # t1 keeps hitting but with new score/tags.
    assert by_tx["t1"].kind is DiffKind.SCORE_CHANGED
    assert by_tx["t1"].candidate.tags == ["mid"]
    # t1 amount 200 also satisfies >=50; only one rule matches.
    assert by_tx["t1"].candidate.hit_rule_ids == ["m"]


def test_multiple_candidate_rules_accumulate_score_and_union_tags(svc):
    _seed(svc)
    # t1 (200) matches amount rule; add a blacklist rule covering A too.
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[
                _amt("m", 100, 4, ("mid",)),
                RuleIn(rule_id="bl", rule_type=RuleType.ADDRESS_BLACKLIST,
                       score=6, tags=["listed"], params={"addresses": ["A"]}),
            ]
        ),
    )
    (diff,) = [d for d in res.changed if d.tx_id == "t1"]
    assert diff.candidate.score == 10
    assert diff.candidate.tags == ["listed", "mid"]
    assert diff.candidate.hit_rule_ids == ["bl", "m"]


def test_dry_run_without_published_baseline_treats_empty_as_no_hits(svc):
    # Only data, no rules published yet.
    _seed(svc)
    svc.store._versions.clear()  # type: ignore[attr-defined]
    res = run_dry_run(svc, DryRunRequest(candidate_rules=[_amt("m", 100)]))
    assert res.baseline_rule_version == 0
    assert {d.tx_id for d in res.new_hits} == {"t1"}
    assert res.lost_hits == []


def test_invalid_candidate_rules_rejected_without_mutation(svc):
    _seed(svc)
    bad = DryRunRequest(
        candidate_rules=[
            RuleIn(rule_id="fo", rule_type=RuleType.HIGH_FANOUT, score=1,
                   tags=[], params={"threshold": 0})
        ]
    )
    with pytest.raises(ValidationError):
        run_dry_run(svc, bad)
    assert svc.current_rule_version().version == 1


def test_trial_equals_post_publish_query_on_same_data_version(svc):
    _seed(svc)
    candidates = [_amt("m", 50, 5, ("mid",)), _loop(score=7)]
    trial = run_dry_run(svc, DryRunRequest(candidate_rules=candidates))
    pinned_dv = trial.data_version

    # Snapshot candidate views for every changed tx.
    expected = {}
    for d in trial.new_hits + trial.lost_hits + trial.changed:
        assert d.candidate is not None
        expected[d.tx_id] = d.candidate

    # New data arriving after the trial must not change the pinned replay.
    svc.import_batch(
        ImportBatchIn(
            batch_id="later",
            transactions=[
                TxIn(tx_id="t9", **{"from": "Z"}, to="Z", amount=9999, ts=9)
            ],
        )
    )
    svc.publish_rules(candidates)  # candidate becomes the real current rules

    for tx_id, view in expected.items():
        q = svc.query_tx(
            tx_id, scope=DataScope(kind="data_version", data_version=pinned_dv)
        )
        assert q.score == view.score
        assert q.tags == view.tags
        assert q.hit_rule_ids == view.hit_rule_ids
        assert q.risk == view.risk


def test_scope_subset_only_covers_requested_tx(svc):
    _seed(svc)
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[_amt("m", 1)],
            scope=DataScope(tx_ids=["t1", "t2"]),
        ),
    )
    compared = {d.tx_id for d in res.new_hits + res.changed}
    assert compared.issubset({"t1", "t2"})
    assert "t3" not in res.covered
    assert set(res.covered) == {"t1", "t2"}
    assert res.in_scope_tx_count == 2


def test_baseline_modes_accepted(svc):
    _seed(svc)
    svc.publish_rules([_amt("v2", 500, 1, ("x",))])  # current = v2
    req = DryRunRequest(
        candidate_rules=[_amt("m", 50)],
        baseline=BaselineSpec(kind=BaselineSpecKind.RULE_VERSION, rule_version=1),
    )
    res = run_dry_run(svc, req)
    assert res.baseline_rule_version == 1
