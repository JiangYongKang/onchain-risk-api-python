"""Capacity limits and partial-coverage annotation."""

from __future__ import annotations

import pytest

from app.config import Limits
from app.dry_run import (
    CandidateRulesLimitExceeded,
    ReportTruncated,
    ScopeLimitExceeded,
    run_dry_run,
)
from app.models import (
    DataScope,
    DryRunRequest,
    ImportBatchIn,
    RuleIn,
    RuleType,
    TxIn,
)

pytestmark = pytest.mark.limits

SMALL = Limits(
    max_candidate_rules=2,
    max_tx_scanned=5,
    max_tx_reported=3,
    max_diff_entries=100,
)


def _loop_rule(i):
    return RuleIn(rule_id=f"r{i}", rule_type=RuleType.LOOP_DETECT,
                  score=1, tags=[], params={})


def _seed(svc, n):
    svc.import_batch(
        ImportBatchIn(
            batch_id="b1",
            transactions=[
                TxIn(tx_id=f"t{i}", **{"from": "A"}, to="B", amount=1, ts=i)
                for i in range(n)
            ],
        )
    )


def test_too_many_candidate_rules_rejected_with_distinct_code(svc):
    _seed(svc, 1)
    req = DryRunRequest(candidate_rules=[_loop_rule(i) for i in range(3)])
    with pytest.raises(CandidateRulesLimitExceeded) as ei:
        run_dry_run(svc, req, limits=SMALL)
    assert ei.value.code == "DRYRUN_RULES_LIMIT_EXCEEDED"
    assert ei.value.status_code == 413
    assert ei.value.details["submitted"] == 3
    assert ei.value.details["limit"] == 2
    # Nothing published.
    assert svc.current_rule_version() is None


def test_scope_over_scan_limit_rejected_with_distinct_code(svc):
    _seed(svc, 10)
    with pytest.raises(ScopeLimitExceeded) as ei:
        run_dry_run(svc, DryRunRequest(candidate_rules=[_loop_rule(0)]),
                    limits=SMALL)
    assert ei.value.code == "DRYRUN_SCOPE_TOO_LARGE"
    assert ei.value.details["in_scope"] == 10
    assert ei.value.details["limit"] == 5


def test_report_truncation_is_marked_with_explicit_coverage(svc):
    _seed(svc, 5)  # within scan limit 5, over report limit 3
    res = run_dry_run(
        svc, DryRunRequest(candidate_rules=[_loop_rule(0)]), limits=SMALL
    )
    assert res.truncated is True
    assert res.complete is False
    assert res.partial_reason == "truncated"
    assert res.covered_tx_count == 3
    assert res.in_scope_tx_count == 5
    assert len(res.covered) == 3
    # Coverage follows deterministic (ts, tx_id) order: t0..t2.
    assert res.covered == ["t0", "t1", "t2"]
    assert any("truncated" in w for w in res.warnings)


def test_hard_no_truncation_mode_raises_distinct_code(svc):
    _seed(svc, 5)
    with pytest.raises(ReportTruncated) as ei:
        run_dry_run(
            svc,
            DryRunRequest(candidate_rules=[_loop_rule(0)]),
            limits=SMALL,
            allow_truncation=False,
        )
    assert ei.value.code == "DRYRUN_REPORT_LIMIT_EXCEEDED"


def test_missing_requested_tx_are_annotated_not_silently_dropped(svc):
    _seed(svc, 2)
    res = run_dry_run(
        svc,
        DryRunRequest(
            candidate_rules=[_loop_rule(0)],
            scope=DataScope(tx_ids=["t0", "ghost", "phantom"]),
        ),
        limits=Limits(100, 100, 100, 100),
    )
    assert res.missing_tx_ids == ["ghost", "phantom"]
    assert res.complete is False
    assert res.partial_reason == "missing_tx"
    assert res.covered_tx_count == 1
    assert res.covered == ["t0"]
    assert any("absent" in w for w in res.warnings)


def test_full_coverage_is_complete(svc):
    _seed(svc, 3)
    res = run_dry_run(
        svc, DryRunRequest(candidate_rules=[_loop_rule(0)]),
        limits=Limits(100, 100, 100, 100),
    )
    assert res.complete is True
    assert res.partial_reason is None
    assert res.truncated is False
    assert res.covered_tx_count == res.in_scope_tx_count == 3


def test_diff_list_truncation_reports_totals(svc):
    # 6 tx all newly hit; per-list cap is 2 -> 2 returned, total 6 visible.
    _seed(svc, 6)
    res = run_dry_run(
        svc,
        DryRunRequest(candidate_rules=[
            RuleIn(rule_id="amt", rule_type=RuleType.AMOUNT_OVER, score=1,
                   tags=[], params={"threshold": 0})
        ]),
        limits=Limits(100, 100, 100, 2),
    )
    assert res.truncated is True
    assert len(res.new_hits) == 2
    assert res.new_hits_count == 6
    assert any("truncated" in w for w in res.warnings)


def test_error_codes_are_stable_and_distinct():
    codes = {
        CandidateRulesLimitExceeded.code,
        ScopeLimitExceeded.code,
        ReportTruncated.code,
    }
    assert codes == {
        "DRYRUN_RULES_LIMIT_EXCEEDED",
        "DRYRUN_SCOPE_TOO_LARGE",
        "DRYRUN_REPORT_LIMIT_EXCEEDED",
    }
