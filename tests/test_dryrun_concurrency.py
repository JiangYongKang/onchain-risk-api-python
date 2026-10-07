"""Dry-run vs concurrent writes/reads: a dry-run must observe one complete,
pinned (data, rule) pair and must never disturb live traffic."""
import threading

from app.models import (
    BaselineSpec,
    DataScope,
    DryRunRequest,
    Rule,
    RuleCondition,
    Transaction,
)
from app.service import RiskService


def tx(i, frm="A", to="B", amount=1.0):
    return Transaction(tx_id=f"tx{i}", from_addr=frm, to_addr=to,
                       asset="ETH", amount=amount, timestamp=1000)


def amount_rule(rule_id, threshold):
    return Rule(rule_id=rule_id, score=50, labels=[rule_id],
                condition=RuleCondition(min_amount=threshold))


def test_dry_run_isolated_from_concurrent_writes_and_queries():
    svc = RiskService()
    svc.import_batch("seed", [tx(i, amount=i) for i in range(200)])
    svc.publish_rules([amount_rule("live", 100)])       # v1

    errors = []
    dry_run_results = []

    def writer():
        try:
            for i in range(15):
                svc.import_batch(f"w{i}", [tx(1000 + i, amount=500)])
                svc.publish_rules([amount_rule(f"live-{i}", 100 + i)])
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(30):
                res = svc.query_risk()
                assert res.data_version >= 1 and res.rule_version >= 1
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def dry_runner():
        try:
            for _ in range(10):
                resp = svc.dry_run(DryRunRequest(
                    candidate_rules=[amount_rule("cand", 50)],
                    baseline=BaselineSpec(type="current"),
                    data_scope=DataScope(type="current")))
                dry_run_results.append(resp)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = ([threading.Thread(target=writer) for _ in range(2)]
               + [threading.Thread(target=reader) for _ in range(3)]
               + [threading.Thread(target=dry_runner) for _ in range(3)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert dry_run_results
    for resp in dry_run_results:
        # Every dry-run pinned a complete (data_version, rule_version) pair;
        # re-running explicitly against those pinned versions must reproduce
        # the exact same diffs -> no torn reads, no version mixing.
        replay = svc.dry_run(DryRunRequest(
            candidate_rules=[amount_rule("cand", 50)],
            baseline=BaselineSpec(type="version",
                                  rule_version=resp.baseline_rule_version),
            data_scope=DataScope(type="version",
                                 data_version=resp.data_version)))
        assert resp.summary == replay.summary
        assert resp.diffs == replay.diffs

    # Live state was never polluted by dry-runs: no extra rule versions,
    # and the active rule set is the last published one.
    versions = svc.list_rule_versions()
    assert all(not v.note.startswith("dry") for v in versions)
    assert len(versions) == 1 + 2 * 15  # seed publish + writers' publishes
    assert svc.query_risk().rule_version == versions[-1].version


def test_dry_run_does_not_populate_versioned_eval_cache_pollution():
    """Candidate evaluations must not be served later as versioned results."""
    svc = RiskService()
    svc.import_batch("b1", [tx(1, amount=150)])
    svc.publish_rules([amount_rule("live", 100)])
    svc.dry_run(DryRunRequest(candidate_rules=[amount_rule("cand", 10)]))
    live = svc.query_risk()
    # live conclusions still come from the published rules only
    assert [h.rule_id for r in live.results for h in r.hits] == ["live"]
