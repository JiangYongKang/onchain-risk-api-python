import pytest

from app.errors import ApiError
from app.models import Rule, Transaction
from app.storage import Store


def tx(i, frm="A", to="B", amount=1.0, ts=1000):
    return Transaction(tx_id=f"tx{i}", from_addr=frm, to_addr=to,
                       asset="ETH", amount=amount, timestamp=ts)


def test_import_batch_assigns_monotonic_data_versions():
    s = Store()
    v1, imported, dups, replayed = s.import_batch("b1", [tx(1), tx(2)])
    assert (v1, imported, dups, replayed) == (1, 2, 0, False)
    v2, imported, _, _ = s.import_batch("b2", [tx(3)])
    assert (v2, imported) == (2, 1)
    assert len(s.snapshot_at_version(1).transactions) == 2
    assert len(s.snapshot_at_version(2).transactions) == 3


def test_import_batch_idempotent_replay():
    s = Store()
    s.import_batch("b1", [tx(1)])
    v, imported, dups, replayed = s.import_batch("b1", [tx(1)])
    assert (v, imported, replayed) == (1, 0, True)
    assert len(s.current_snapshot().transactions) == 1


def test_import_batch_conflict_on_different_payload():
    s = Store()
    s.import_batch("b1", [tx(1)])
    with pytest.raises(ApiError) as ei:
        s.import_batch("b1", [tx(9)])
    assert ei.value.code == "BATCH_CONFLICT"


def test_import_skips_duplicate_tx_ids_across_batches():
    s = Store()
    s.import_batch("b1", [tx(1), tx(2)])
    _, imported, dups, _ = s.import_batch("b2", [tx(2), tx(3)])
    assert (imported, dups) == (1, 1)
    assert [t.tx_id for t in s.current_snapshot().transactions] == ["tx1", "tx2", "tx3"]


def test_publish_and_rollback_rules():
    s = Store()
    r1 = s.publish_rules([Rule(rule_id="r1", score=10)], note="v1")
    r2 = s.publish_rules([Rule(rule_id="r2", score=20)], note="v2")
    assert (r1.version, r2.version) == (1, 2)
    assert s.current_ruleset().version == 2
    r3 = s.rollback_rules(1)
    assert r3.version == 3
    assert s.current_ruleset().version == 3
    # rollback copies content, history untouched
    assert s.current_ruleset().rules[0].rule_id == "r1"
    assert s.ruleset_at_version(2).rules[0].rule_id == "r2"


def test_unknown_versions_raise_stable_codes():
    s = Store()
    with pytest.raises(ApiError) as e1:
        s.snapshot_at_version(7)
    assert e1.value.code == "DATA_VERSION_NOT_FOUND"
    with pytest.raises(ApiError) as e2:
        s.ruleset_at_version(7)
    assert e2.value.code == "RULE_VERSION_NOT_FOUND"


def test_as_of_resolution():
    s = Store()
    s.import_batch("b1", [tx(1)])
    t1 = s.current_snapshot().created_at
    s.import_batch("b2", [tx(2)])
    s.publish_rules([Rule(rule_id="r1")])
    tr1 = s.current_ruleset().created_at
    s.publish_rules([Rule(rule_id="r2")])
    assert s.snapshot_as_of(t1).data_version == 1
    assert s.ruleset_as_of(tr1).rules[0].rule_id == "r1"
    with pytest.raises(ApiError) as ei:
        s.snapshot_as_of(t1 - 100)
    assert ei.value.code == "INVALID_AS_OF"
