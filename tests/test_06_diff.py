"""Version diff: new/lost hits, score/tag/rule-set changes, attribution."""

from __future__ import annotations

import pytest

from app.dry_run import run_dry_run
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

pytestmark = pytest.mark.diff


def _amt(rule_id, threshold, score=10, tags=()):
    return RuleIn(rule_id=rule_id, rule_type=RuleType.AMOUNT_OVER,
                  score=score, tags=list(tags), params={"threshold": threshold})


def _seed(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id="keep", **{"from": "A"}, to="B", amount=200, ts=1),
                TxIn(tx_id="loose", **{"from": "C"}, to="D", amount=200, ts=2),
                TxIn(tx_id="new", **{"from": "E"}, to="F", amount=20, ts=3),
                TxIn(tx_id="quiet", **{"from": "G"}, to="H", amount=5, ts=4),
            ],
        )
    )
    svc.publish_rules(
        [
            _amt("old-big", 100, 10, ("big",)),
            RuleIn(rule_id="old-black", rule_type=RuleType.ADDRESS_BLACKLIST,
                   score=3, tags=["listed"], params={"addresses": ["C"]}),
        ]
    )


def test_diff_classifies_new_lost_and_changed(svc):
    _seed(svc)
    # Candidate: threshold 50 (loses "loose" 200? no -> 200>=50 keeps it; but
    # we drop the blacklist so "loose" stays via amount). Build carefully:
    candidates = [
        _amt("new-mid", 10, 4, ("mid",)),      # catches "new"(20); "quiet"(5) no
    ]
    res = run_dry_run(svc, DryRunRequest(candidate_rules=candidates))

    ids_new = {d.tx_id for d in res.new_hits}
    ids_lost = {d.tx_id for d in res.lost_hits}
    ids_changed = {d.tx_id for d in res.changed}

    # "new" is flagged only by candidate; "quiet" by neither.
    assert ids_new == {"new"}
    # "loose" loses the blacklist contribution but amount still flags it; so it
    # is a changed (score/tags) not a lost hit. Build a pure-lost case below.
    assert "loose" in ids_changed
    assert "quiet" not in (ids_new | ids_lost | ids_changed)
    assert res.unchanged_count >= 0


def test_pure_lost_hit_when_candidate_flags_nothing(svc):
    _seed(svc)
    # Very high threshold + no blacklist -> "loose"/"keep" drop off entirely.
    res = run_dry_run(
        svc, DryRunRequest(candidate_rules=[_amt("future", 10000, 1)])
    )
    assert {d.tx_id for d in res.lost_hits} == {"keep", "loose"}
    loose = next(d for d in res.lost_hits if d.tx_id == "loose")
    assert loose.kind is DiffKind.LOST_HIT
    assert loose.candidate.risk is False and loose.baseline.risk is True


def test_each_diff_attributes_candidate_and_baseline_rules(svc):
    _seed(svc)
    # "loose" baseline = amount(10) + blacklist(3) = 13, tags big+listed.
    # Candidate = amount(4, mid) only.
    res = run_dry_run(svc, DryRunRequest(candidate_rules=[_amt("new-mid", 100, 4, ("mid",))]))
    loose = next(d for d in res.changed if d.tx_id == "loose")
    assert [c.rule_id for c in loose.candidate_contributors] == ["new-mid"]
    assert sorted(c.rule_id for c in loose.baseline_contributors) == [
        "old-big", "old-black"
    ]
    assert loose.score_delta == 4 - 13
    assert loose.added_tags == ["mid"]
    assert loose.removed_tags == ["big", "listed"]


def test_hit_rule_set_change_with_same_score_and_tags(svc):
    # Two different rules, same score/tags: outcome identical but the rule set
    # differs -> must still be reported and attributed.
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)
            ],
        )
    )
    svc.publish_rules([_amt("rule-a", 100, 7, ("x",))])
    res = run_dry_run(svc, DryRunRequest(candidate_rules=[_amt("rule-b", 100, 7, ("x",))]))
    (d,) = res.changed
    assert d.kind is DiffKind.HIT_RULES_CHANGED
    assert d.baseline_contributors[0].rule_id == "rule-a"
    assert d.candidate_contributors[0].rule_id == "rule-b"
    assert d.score_delta == 0 and d.added_tags == [] and d.removed_tags == []


def test_tags_only_change_classification(svc):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)
            ],
        )
    )
    svc.publish_rules([_amt("a", 100, 5, ("one",))])
    res = run_dry_run(svc, DryRunRequest(candidate_rules=[_amt("b", 100, 5, ("two",))]))
    (d,) = res.changed
    assert d.kind is DiffKind.TAGS_CHANGED
    assert d.added_tags == ["two"] and d.removed_tags == ["one"]


def test_identical_candidate_and_baseline_is_all_unchanged(svc):
    _seed(svc)
    same = list(svc.current_rule_version().rules)
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[
                RuleIn(rule_id=r.rule_id, rule_type=r.rule_type, score=r.score,
                       tags=list(r.tags), params=dict(r.params))
                for r in same
            ]
        ),
    )
    assert res.new_hits == [] and res.lost_hits == [] and res.changed == []
    # All four covered transactions (including the never-risky "quiet") are
    # unchanged.
    assert res.unchanged_count == 4
    assert res.unchanged_count == res.covered_tx_count


def test_explicit_historical_rule_version_as_baseline(svc):
    _seed(svc)
    svc.publish_rules([_amt("v2", 500, 1, ("z",))])  # current flags nothing
    # Compare candidate against v1 even though current is v2.
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[_amt("cand", 1000, 9)],
            baseline=BaselineSpec(kind=BaselineSpecKind.RULE_VERSION,
                                  rule_version=1),
        ),
    )
    assert res.baseline_rule_version == 1
    # Against v1: keep/loose were risky(13/10) but candidate threshold 1000
    # flags nothing -> lost hits relative to v1.
    assert {d.tx_id for d in res.lost_hits} == {"keep", "loose"}


def test_historical_instant_baseline_is_self_consistent(svc):
    # b1@1000, v1@1100 (threshold100), b2@1200, v2@1300 (threshold500).
    svc.import_batch(ImportBatchIn(
        batch_id="b1", imported_at=1000,
        transactions=[TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)]))
    svc.publish_rules([_amt("v1", 100, 10, ("big",))], created_at=1100)
    svc.import_batch(ImportBatchIn(
        batch_id="b2", imported_at=1200,
        transactions=[TxIn(tx_id="t2", **{"from": "B"}, to="C", amount=200, ts=2)]))
    svc.publish_rules([_amt("v2", 500, 3, ("huge",))], created_at=1300)

    # No explicit scope -> data auto-pinned to as_of=1150 (only b1, rule v1).
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[_amt("cand", 150, 9, ("x",))],
            baseline=BaselineSpec(kind=BaselineSpecKind.AS_OF, as_of=1150),
        ),
    )
    assert res.baseline_rule_version == 1
    assert res.data_version == 1
    assert res.total_tx_count == 1  # t2 not present at that time
    assert all(d.tx_id != "t2" for d in res.new_hits + res.lost_hits + res.changed)
    assert any("as_of" in w for w in res.warnings)


def test_explicit_scope_overrides_auto_pin(svc):
    svc.import_batch(ImportBatchIn(
        batch_id="b1", imported_at=1000,
        transactions=[TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=200, ts=1)]))
    svc.publish_rules([_amt("v1", 100, 10, ("big",))], created_at=1100)
    svc.import_batch(ImportBatchIn(
        batch_id="b2", imported_at=1200,
        transactions=[TxIn(tx_id="t2", **{"from": "B"}, to="C", amount=200, ts=2)]))
    svc.publish_rules([_amt("v2", 500, 3, ("huge",))], created_at=1300)

    # Old rule (v1) deliberately applied to CURRENT data: caller's explicit
    # choice; both sides still use internally consistent rule versions.
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[_amt("cand", 1000, 9)],
            baseline=BaselineSpec(kind=BaselineSpecKind.RULE_VERSION,
                                  rule_version=1),
            scope=DataScope(kind="current"),
        ),
    )
    assert res.baseline_rule_version == 1
    assert res.data_version == 2
    assert {d.tx_id for d in res.lost_hits} == {"t1", "t2"}
