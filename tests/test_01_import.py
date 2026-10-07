"""Regression: batch import idempotency and conflicts."""

from __future__ import annotations

import pytest

from app.errors import ConflictError
from app.models import EdgeIn, ImportBatchIn, TxIn

pytestmark = pytest.mark.regression


def _batch(bid="b1", amount=200):
    return ImportBatchIn(
        batch_id=bid,
        transactions=[TxIn(tx_id="t1", **{"from": "A"}, to="B", amount=amount, ts=1)],
    )


def test_first_import_applies(svc):
    r = svc.import_batch(_batch())
    assert r.imported is True
    assert r.deduped is False
    assert r.data_version == 1


def test_duplicate_import_is_idempotent_even_without_timestamp(svc):
    first = svc.import_batch(_batch())
    # Retry omits imported_at (server clock advances) but content is identical.
    second = svc.import_batch(_batch())
    assert second.imported is False
    assert second.deduped is True
    # No new data version created.
    assert second.data_version == first.data_version == 1


def test_same_batch_id_different_content_conflicts(svc):
    svc.import_batch(_batch(amount=200))
    with pytest.raises(ConflictError) as ei:
        svc.import_batch(_batch(amount=999))
    assert ei.value.code == "CONFLICT"


def test_distinct_batches_grow_data_versions(svc):
    b2 = ImportBatchIn(
        batch_id="b2",
        transactions=[TxIn(tx_id="t2", **{"from": "B"}, to="C", amount=10, ts=2)],
    )
    svc.import_batch(_batch())
    assert svc.import_batch(b2).data_version == 2


def test_duplicate_tx_id_inside_one_batch_rejected(svc):
    from app.errors import ValidationError

    payload = ImportBatchIn(
        batch_id="bx",
        transactions=[
            TxIn(tx_id="dup", **{"from": "A"}, to="B", amount=1, ts=1),
            TxIn(tx_id="dup", **{"from": "A"}, to="C", amount=1, ts=2),
        ],
    )
    with pytest.raises(ValidationError):
        svc.import_batch(payload)


def test_explicit_edges_are_imported(svc):
    payload = ImportBatchIn(batch_id="be", edges=[EdgeIn(**{"from": "X"}, to="Y")])
    r = svc.import_batch(payload)
    assert r.data_version == 1
