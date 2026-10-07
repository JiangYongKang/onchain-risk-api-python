"""Versioned, append-only storage with copy-on-write snapshots.

Writes (imports, rule publishes/rollbacks) append new versions and atomically
swap the "current" pointer, so readers (risk queries, dry-runs) always see a
complete, consistent version and never block writers.
"""
from __future__ import annotations

import threading

from app.models import Rule, Transaction


class Snapshot:
    """An immutable view of the transaction data at one data version."""

    def __init__(self, data_version: int, created_at: float,
                 transactions: tuple[Transaction, ...]):
        self.data_version = data_version
        self.created_at = created_at
        self.transactions = transactions


class RuleSet:
    """An immutable, versioned set of rules."""

    def __init__(self, version: int, rules: tuple[Rule, ...],
                 created_at: float, note: str):
        self.version = version
        self.rules = rules
        self.created_at = created_at
        self.note = note


class Store:
    """Holds all data versions, rule versions and idempotency records."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshots: list[Snapshot] = []
        self._rulesets: list[RuleSet] = []
        self._batches: dict[str, tuple[int, list[Transaction]]] = {}
        self._current_ruleset_version: int = 0

    # -- writes ------------------------------------------------------------
    def import_batch(self, batch_id: str,
                     transactions: list[Transaction]) -> tuple[int, int, int, bool]:
        """Append a batch; returns (data_version, imported, duplicates, replayed)."""
        raise NotImplementedError

    def publish_rules(self, rules: list[Rule], note: str) -> RuleSet:
        raise NotImplementedError

    def rollback_rules(self, version: int) -> RuleSet:
        raise NotImplementedError

    # -- reads ---------------------------------------------------------------
    def current_snapshot(self) -> Snapshot:
        raise NotImplementedError

    def snapshot_at_version(self, data_version: int) -> Snapshot:
        raise NotImplementedError

    def snapshot_as_of(self, ts: float) -> Snapshot:
        raise NotImplementedError

    def current_ruleset(self) -> RuleSet:
        raise NotImplementedError

    def ruleset_at_version(self, version: int) -> RuleSet:
        raise NotImplementedError

    def ruleset_as_of(self, ts: float) -> RuleSet:
        raise NotImplementedError

    def list_rule_versions(self) -> list[RuleSet]:
        raise NotImplementedError
