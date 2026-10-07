"""Service-level tests: imports, rule lifecycle, historical replay, graph rules."""
import threading

import pytest

from app.errors import ApiError
from app.models import Rule, RuleCondition, Transaction
from app.service import RiskService


def tx(i, frm="A", to="B", amount=1.0, asset="ETH"):
    return Transaction(tx_id=f"tx{i}", from_addr=frm, to_addr=to,
                       asset=asset, amount=amount, timestamp=1000)


def big_amount_rule(rule_id="big", threshold=100):
    return Rule(rule_id=rule_id, score=50, labels=["big"],
                condition=RuleCondition(min_amount=threshold))


def make_service():
    svc = RiskService()
    svc.import_batch("b1", [tx(1, amount=150), tx(2, amount=10)])
    return svc


def test_query_risk_current():
    svc = make_service()
    svc.publish_rules([big_amount_rule()])
    res = svc.query_risk()
    by_id = {r.tx_id: r for r in res.results}
    assert by_id["tx1"].score == 50 and by_id["tx1"].labels == ["big"]
    assert by_id["tx2"].score == 0 and by_id["tx2"].hits == []
    assert res.rule_version == 1 and res.data_version == 1


def test_rollback_restores_previous_conclusions():
    svc = make_service()
    svc.publish_rules([big_amount_rule("big", 100)])          # v1
    svc.publish_rules([big_amount_rule("big", 1000)])         # v2: nothing hits
    assert all(r.score == 0 for r in svc.query_risk().results)
    info = svc.rollback_rules(1)                              # v3 = copy of v1
    assert info.active and info.version == 3
    res = svc.query_risk()
    assert {r.tx_id for r in res.results if r.score > 0} == {"tx1"}
    # history intact: v2 conclusions still reproducible
    res_v2 = svc.query_risk(rule_version=2)
    assert all(r.score == 0 for r in res_v2.results)


def test_historical_replay_as_of_is_self_consistent():
    svc = RiskService()
    svc.import_batch("b1", [tx(1, amount=150)])
    svc.publish_rules([big_amount_rule("big", 100)])          # v1
    t_mid = svc.query_risk().results and __import__("time").time()
    svc.import_batch("b2", [tx(2, amount=200)])
    svc.publish_rules([big_amount_rule("big", 1000)])         # v2
    replay = svc.query_risk(as_of=t_mid)
    assert replay.data_version == 1 and replay.rule_version == 1
    assert [r.tx_id for r in replay.results] == ["tx1"]
    assert replay.results[0].score == 50


def test_as_of_cannot_mix_with_explicit_versions():
    svc = make_service()
    svc.publish_rules([big_amount_rule()])
    with pytest.raises(ApiError) as ei:
        svc.query_risk(as_of=1.0, rule_version=1)
    assert ei.value.code == "VALIDATION_ERROR"


def test_data_version_pinned_query_ignores_later_imports():
    svc = make_service()
    svc.publish_rules([big_amount_rule()])
    svc.import_batch("b2", [tx(3, amount=999)])
    res = svc.query_risk(data_version=1)
    assert res.data_version == 1
    assert [r.tx_id for r in res.results] == ["tx1", "tx2"]


def test_cycle_and_fanout_rules_end_to_end():
    svc = RiskService()
    svc.import_batch("cycle", [tx(1, "A", "B"), tx(2, "B", "C"), tx(3, "C", "A")])
    svc.import_batch("fanout", [tx(4 + i, "H", f"N{i}") for i in range(5)])
    svc.publish_rules([
        Rule(rule_id="cyc", score=30, labels=["cycle"],
             condition=RuleCondition(in_cycle=True)),
        Rule(rule_id="hub", score=70, labels=["high-fanout"],
             condition=RuleCondition(min_fan_out=5)),
    ])
    res = svc.query_risk()
    by_id = {r.tx_id: r for r in res.results}
    assert all(by_id[f"tx{i}"].labels == ["cycle"] for i in (1, 2, 3))
    assert all(by_id[f"tx{i}"].labels == ["high-fanout"] for i in range(4, 9))


def test_concurrent_reads_and_writes_keep_consistent_versions():
    svc = make_service()
    svc.publish_rules([big_amount_rule()])
    errors = []

    def writer():
        try:
            for i in range(20):
                svc.import_batch(f"w{i}", [tx(1000 + i, amount=500)])
                svc.publish_rules([big_amount_rule("big", 100 + i)])
        except ApiError as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(50):
                res = svc.query_risk()
                # every read must see a complete, self-consistent version
                assert res.data_version >= 1 and res.rule_version >= 1
                assert len(res.results) == len({r.tx_id for r in res.results})
        except AssertionError as e:  # pragma: no cover
            errors.append(e)

    threads = ([threading.Thread(target=writer) for _ in range(3)]
               + [threading.Thread(target=reader) for _ in range(5)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
