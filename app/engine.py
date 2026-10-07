"""Pure, deterministic evaluation engine.

The same functions back live risk queries, historical replay and dry-runs,
which is what guarantees "dry-run output == post-publish output" for the same
(data version, rule set) pair.
"""
from __future__ import annotations

from app.models import DiffEntry, Rule, Transaction, TxRiskResult
from app.storage import Snapshot


class GraphFeatures:
    """Derived address-graph features for one snapshot."""

    def __init__(self) -> None:
        self.fan_out: dict[str, int] = {}
        self.fan_in: dict[str, int] = {}
        self.cyclic_addresses: frozenset[str] = frozenset()


def build_graph_features(transactions: tuple[Transaction, ...]) -> GraphFeatures:
    raise NotImplementedError


def evaluate_transaction(tx: Transaction, features: GraphFeatures,
                         rules: tuple[Rule, ...]) -> TxRiskResult:
    raise NotImplementedError


def evaluate_snapshot(features: GraphFeatures,
                      transactions: tuple[Transaction, ...],
                      rules: tuple[Rule, ...]) -> dict[str, TxRiskResult]:
    raise NotImplementedError


def diff_results(candidate: dict[str, TxRiskResult],
                 baseline: dict[str, TxRiskResult]) -> list[DiffEntry]:
    raise NotImplementedError
