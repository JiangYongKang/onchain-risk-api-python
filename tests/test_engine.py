from app.engine import (
    build_graph_features,
    diff_results,
    evaluate_snapshot,
    evaluate_transaction,
)
from app.models import Rule, RuleCondition, Transaction


def tx(i, frm, to, amount=1.0, asset="ETH"):
    return Transaction(tx_id=f"tx{i}", from_addr=frm, to_addr=to,
                       asset=asset, amount=amount, timestamp=1000)


def test_fan_out_and_fan_in():
    txs = tuple([tx(1, "A", "B"), tx(2, "A", "C"), tx(3, "A", "D"), tx(4, "B", "C")])
    f = build_graph_features(txs)
    assert f.fan_out["A"] == 3
    assert f.fan_in["C"] == 2
    assert f.fan_out.get("D", 0) == 0


def test_cycle_detection():
    txs = tuple([tx(1, "A", "B"), tx(2, "B", "C"), tx(3, "C", "A"), tx(4, "X", "Y")])
    f = build_graph_features(txs)
    assert {"A", "B", "C"} <= set(f.cyclic_addresses)
    assert "X" not in f.cyclic_addresses and "Y" not in f.cyclic_addresses


def test_self_loop_is_a_cycle():
    f = build_graph_features(tuple([tx(1, "A", "A")]))
    assert "A" in f.cyclic_addresses


def test_rule_matching_amount_asset_and_addresses():
    rule = Rule(rule_id="r1", score=50, labels=["big"],
                condition=RuleCondition(min_amount=100, assets=["ETH"]))
    f = build_graph_features(tuple())
    assert evaluate_transaction(tx(1, "A", "B", amount=150), f, (rule,)).score == 50
    assert evaluate_transaction(tx(2, "A", "B", amount=50), f, (rule,)).score == 0
    assert evaluate_transaction(tx(3, "A", "B", amount=150, asset="BTC"), f, (rule,)).score == 0


def test_rule_matching_graph_features():
    fanout_rule = Rule(rule_id="fo", score=10, labels=["fanout"],
                       condition=RuleCondition(min_fan_out=3))
    cycle_rule = Rule(rule_id="cy", score=20, labels=["cycle"],
                      condition=RuleCondition(in_cycle=True))
    txs = tuple([tx(1, "A", "B"), tx(2, "A", "C"), tx(3, "A", "D"),
                 tx(4, "B", "A")])  # A<->B cycle, A fan-out 3
    f = build_graph_features(txs)
    res = evaluate_snapshot(f, txs, (fanout_rule, cycle_rule))
    assert res["tx1"].score == 30
    assert set(res["tx1"].labels) == {"fanout", "cycle"}
    assert res["tx4"].score == 20  # in cycle, but B fan-out is 1
    assert [h.rule_id for h in res["tx1"].hits] == ["fo", "cy"]


def test_labels_deduped_in_order():
    r1 = Rule(rule_id="r1", score=1, labels=["x", "y"])
    r2 = Rule(rule_id="r2", score=1, labels=["y", "z"])
    f = build_graph_features(tuple())
    res = evaluate_transaction(tx(1, "A", "B"), f, (r1, r2))
    assert res.labels == ["x", "y", "z"]


def test_diff_results_added_removed_changed():
    f = build_graph_features(tuple())
    t = tx(1, "A", "B")
    hit = Rule(rule_id="new", score=5, labels=["l"])
    cand = {"t1": evaluate_transaction(t, f, (hit,))}
    base = {"t1": evaluate_transaction(t, f, ())}
    diffs = diff_results(cand, base)
    assert len(diffs) == 1 and diffs[0].change_type == "added"
    assert [h.rule_id for h in diffs[0].candidate.hits] == ["new"]
    assert diffs[0].baseline.hits == []

    diffs2 = diff_results(base, cand)
    assert diffs2[0].change_type == "removed"
    assert [h.rule_id for h in diffs2[0].baseline.hits] == ["new"]

    hit2 = Rule(rule_id="new", score=9, labels=["l2"])
    cand2 = {"t1": evaluate_transaction(t, f, (hit2,))}
    diffs3 = diff_results(cand2, cand)
    assert diffs3[0].change_type == "changed"
    assert diffs3[0].candidate.score == 9 and diffs3[0].baseline.score == 5

    assert diff_results(cand, cand) == []
