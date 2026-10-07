"""Dry-run tests: diffs, baselines, scoping, limits, coverage, isolation."""
import time

import pytest

from app.errors import ApiError
from app.models import (
    BaselineSpec,
    DataScope,
    DryRunRequest,
    Rule,
    RuleCondition,
    Transaction,
)
from app.service import MAX_CANDIDATE_RULES, MAX_DRYRUN_TXS, RiskService


def tx(i, frm="A", to="B", amount=1.0, asset="ETH"):
    return Transaction(tx_id=f"tx{i}", from_addr=frm, to_addr=to,
                       asset=asset, amount=amount, timestamp=1000)


def amount_rule(rule_id, threshold, score=50, labels=None):
    return Rule(rule_id=rule_id, score=score, labels=labels or [rule_id],
                condition=RuleCondition(min_amount=threshold))


def make_service():
    svc = RiskService()
    svc.import_batch("b1", [tx(1, amount=150), tx(2, amount=10), tx(3, amount=80)])
    svc.publish_rules([amount_rule("live-big", 100)])   # v1: hits tx1
    return svc


def dry(**kwargs):
    return DryRunRequest(**kwargs)


def test_dry_run_diff_added_removed_changed_with_attribution():
    svc = make_service()
    resp = svc.dry_run(dry(candidate_rules=[
        amount_rule("cand-mid", 50),          # hits tx1, tx3 -> tx3 is added
        amount_rule("cand-huge", 1000),       # hits nothing
    ]))
    assert resp.data_version == 1 and resp.baseline_rule_version == 1
    s = resp.summary
    assert (s.evaluated, s.candidate_hits, s.baseline_hits) == (3, 2, 1)
    assert (s.added, s.removed, s.changed) == (1, 0, 1)
    by_id = {d.tx_id: d for d in resp.diffs}
    # tx3: newly hit by candidate rule cand-mid
    assert by_id["tx3"].change_type == "added"
    assert [h.rule_id for h in by_id["tx3"].candidate.hits] == ["cand-mid"]
    assert by_id["tx3"].baseline.hits == []
    # tx1: hit on both sides, score/labels differ; both sides attribute rules
    assert by_id["tx1"].change_type == "changed"
    assert [h.rule_id for h in by_id["tx1"].candidate.hits] == ["cand-mid"]
    assert [h.rule_id for h in by_id["tx1"].baseline.hits] == ["live-big"]
    assert by_id["tx1"].baseline.score == 50


def test_dry_run_removed():
    svc = make_service()
    resp = svc.dry_run(dry(candidate_rules=[amount_rule("cand-huge", 1000)]))
    assert resp.summary.removed == 1
    d = resp.diffs[0]
    assert d.tx_id == "tx1" and d.change_type == "removed"
    assert [h.rule_id for h in d.baseline.hits] == ["live-big"]
    assert d.candidate.hits == []


def test_dry_run_does_not_touch_live_state():
    svc = make_service()
    before_versions = svc.list_rule_versions()
    before_query = svc.query_risk()
    svc.dry_run(dry(candidate_rules=[amount_rule("cand-mid", 50)]))
    assert svc.list_rule_versions() == before_versions
    assert svc.query_risk() == before_query


def test_dry_run_matches_post_publish_results_on_same_data_version():
    svc = make_service()
    candidate = [amount_rule("cand-mid", 50), amount_rule("cand-low", 5)]
    resp = svc.dry_run(dry(candidate_rules=candidate))
    cand_side = {d.tx_id: d.candidate for d in resp.diffs}
    # really publish the candidate rules, then query the SAME data version
    svc.publish_rules(candidate)
    live = svc.query_risk(data_version=resp.data_version)
    assert live.rule_version == 2
    for r in live.results:
        if r.tx_id in cand_side:
            assert r.score == cand_side[r.tx_id].score
            assert r.labels == cand_side[r.tx_id].labels
            assert [h.rule_id for h in r.hits] == \
                [h.rule_id for h in cand_side[r.tx_id].hits]


def test_dry_run_baseline_by_historical_rule_version():
    svc = make_service()
    svc.publish_rules([amount_rule("live-big", 1000)])  # v2: nothing hits live
    resp = svc.dry_run(dry(
        candidate_rules=[amount_rule("cand-mid", 50)],
        baseline=BaselineSpec(type="version", rule_version=1)))
    assert resp.baseline_rule_version == 1
    # compared against v1 (not current v2): tx1 hit on both sides -> changed
    assert {d.tx_id: d.change_type for d in resp.diffs} == {
        "tx1": "changed", "tx3": "added"}


def test_dry_run_historical_as_of_is_self_consistent():
    svc = RiskService()
    svc.import_batch("b1", [tx(1, amount=150)])
    svc.publish_rules([amount_rule("r-v1", 100)])       # v1
    t_hist = time.time()
    time.sleep(0.01)
    svc.import_batch("b2", [tx(2, amount=200)])
    svc.publish_rules([amount_rule("r-v2", 1000)])      # v2
    resp = svc.dry_run(dry(
        candidate_rules=[amount_rule("cand", 100)],
        baseline=BaselineSpec(type="as_of", as_of=t_hist),
        data_scope=DataScope(type="as_of", as_of=t_hist)))
    # pinned to the historical pair (data v1, rules v1): only tx1 in scope
    assert resp.data_version == 1 and resp.baseline_rule_version == 1
    assert resp.summary.evaluated == 1
    assert resp.diffs[0].tx_id == "tx1"
    assert [h.rule_id for h in resp.diffs[0].baseline.hits] == ["r-v1"]


def test_dry_run_scope_by_tx_ids():
    svc = make_service()
    resp = svc.dry_run(dry(
        candidate_rules=[amount_rule("cand-mid", 50)],
        data_scope=DataScope(tx_ids=["tx3"])))
    assert resp.summary.evaluated == 1
    assert [d.tx_id for d in resp.diffs] == ["tx3"]
    assert resp.coverage.full and resp.coverage.total == 1


def test_dry_run_partial_coverage_is_annotated():
    svc = make_service()
    resp = svc.dry_run(dry(
        candidate_rules=[amount_rule("cand-low", 1)],
        data_scope=DataScope(limit=2)))
    assert not resp.coverage.full
    assert resp.coverage.reason == "SCOPE_LIMIT"
    assert resp.coverage.scanned == 2 and resp.coverage.total == 3
    assert resp.summary.evaluated == 2


def test_dry_run_max_diffs_truncation():
    svc = make_service()
    resp = svc.dry_run(dry(candidate_rules=[amount_rule("cand-low", 1)],
                           max_diffs=1))
    assert resp.diffs_truncated and len(resp.diffs) == 1
    # summary still reflects the full diff set
    assert resp.summary.added + resp.summary.changed >= 2


def test_dry_run_candidate_rule_limit():
    svc = make_service()
    with pytest.raises(ApiError) as ei:
        svc.dry_run(dry(candidate_rules=[
            amount_rule(f"r{i}", 1) for i in range(MAX_CANDIDATE_RULES + 1)]))
    assert ei.value.code == "DRY_RUN_LIMIT_EXCEEDED"


def test_dry_run_tx_limit_requires_narrowing():
    svc = RiskService()
    svc.import_batch("bulk", [tx(i, amount=1) for i in range(MAX_DRYRUN_TXS + 1)])
    svc.publish_rules([amount_rule("r", 1)])
    with pytest.raises(ApiError) as ei:
        svc.dry_run(dry(candidate_rules=[amount_rule("c", 1)]))
    assert ei.value.code == "DRY_RUN_LIMIT_EXCEEDED"
    # ...but an explicit limit makes it a partial-coverage run, not an error
    resp = svc.dry_run(dry(candidate_rules=[amount_rule("c", 1)],
                           data_scope=DataScope(limit=10)))
    assert resp.coverage.scanned == 10 and not resp.coverage.full


def test_dry_run_validation_errors_are_stable():
    svc = make_service()
    with pytest.raises(ApiError) as e1:
        svc.dry_run(dry(candidate_rules=[]))
    assert e1.value.code == "VALIDATION_ERROR"
    with pytest.raises(ApiError) as e2:
        svc.dry_run(dry(candidate_rules=[amount_rule("c", 1)],
                        baseline=BaselineSpec(type="version", rule_version=99)))
    assert e2.value.code == "RULE_VERSION_NOT_FOUND"
    with pytest.raises(ApiError) as e3:
        svc.dry_run(dry(candidate_rules=[amount_rule("c", 1)],
                        data_scope=DataScope(type="version", data_version=99)))
    assert e3.value.code == "DATA_VERSION_NOT_FOUND"
