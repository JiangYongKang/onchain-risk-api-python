"""Versioned, append-only storage with copy-on-write snapshots.

Writes (imports, rule publishes/rollbacks) append new versions and atomically
swap the "current" pointer under a lock, so readers (risk queries, dry-runs)
always see a complete, consistent version and never block writers. Snapshots
and rule sets are immutable once published, which is what makes concurrent
dry-runs safe: a dry-run pins its snapshot/ruleset references and works on
them while writes keep appending new versions.
"""
from __future__ import annotations

import bisect
import threading
import time

from app.errors import (
    BATCH_CONFLICT,
    DATA_VERSION_NOT_FOUND,
    INVALID_AS_OF,
    RULE_VERSION_NOT_FOUND,
    ApiError,
)
from app.models import Rule, Transaction


class Snapshot:
    """An immutable view of the transaction data at one data version."""

    def __init__(self, data_version: int, created_at: float,
                 transactions: tuple[Transaction, ...]):
        self.data_version = data_version
        self.created_at = created_at
        self.transactions = transactions  # tuple -> immutable


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
        # version -> Snapshot (versions are 1..N, index = version - 1)
        self._snapshots: list[Snapshot] = []
        # version -> RuleSet (versions are 1..N, index = version - 1)
        self._rulesets: list[RuleSet] = []
        # batch_id -> (data_version, canonical payload) for idempotent import
        self._batches: dict[str, tuple[int, tuple[Transaction, ...]]] = {}
        self._current_ruleset_version: int = 0  # 0 = no rules published yet

    # -- writes --------------------------------------------------------------
    def import_batch(self, batch_id: str,
                     transactions: list[Transaction]) -> tuple[int, int, int, bool]:
        """Append a batch of transactions.

        Idempotent on ``batch_id``: re-importing the same batch with an
        identical payload replays the original result without duplicating
        data; the same batch_id with a different payload is a conflict.
        Returns (data_version, imported, duplicates, replayed).
        """
        payload = tuple(transactions)
        with self._lock:
            existing = self._batches.get(batch_id)
            if existing is not None:
                data_version, canonical = existing
                if canonical != payload:
                    raise ApiError(
                        BATCH_CONFLICT,
                        f"batch {batch_id!r} already imported with a different payload",
                        status=409,
                    )
                return data_version, 0, len(payload), True

            prev_txs = self._snapshots[-1].transactions if self._snapshots else ()
            seen = {tx.tx_id for tx in prev_txs}
            imported = 0
            duplicates = 0
            new_txs: list[Transaction] = []
            for tx in payload:
                if tx.tx_id in seen:
                    duplicates += 1
                    continue
                seen.add(tx.tx_id)
                new_txs.append(tx)
                imported += 1

            data_version = len(self._snapshots) + 1
            snapshot = Snapshot(
                data_version=data_version,
                created_at=time.time(),
                transactions=prev_txs + tuple(new_txs),
            )
            self._snapshots.append(snapshot)
            self._batches[batch_id] = (data_version, payload)
            return data_version, imported, duplicates, False

    def publish_rules(self, rules: list[Rule], note: str = "") -> RuleSet:
        """Publish a new rule version and make it active atomically."""
        with self._lock:
            version = len(self._rulesets) + 1
            ruleset = RuleSet(version=version, rules=tuple(rules),
                              created_at=time.time(), note=note)
            self._rulesets.append(ruleset)
            self._current_ruleset_version = version
            return ruleset

    def rollback_rules(self, version: int) -> RuleSet:
        """Point the active rule set back at a historical version.

        Rollback does not rewrite history: it appends a new rule version whose
        content is copied from the target version, so every version remains
        reproducible.
        """
        with self._lock:
            target = self.ruleset_at_version(version)
            version_new = len(self._rulesets) + 1
            ruleset = RuleSet(
                version=version_new,
                rules=target.rules,
                created_at=time.time(),
                note=f"rollback to v{version}",
            )
            self._rulesets.append(ruleset)
            self._current_ruleset_version = version_new
            return ruleset

    # -- reads ---------------------------------------------------------------
    def current_snapshot(self) -> Snapshot:
        with self._lock:
            if not self._snapshots:
                return Snapshot(data_version=0, created_at=0.0, transactions=())
            return self._snapshots[-1]

    def snapshot_at_version(self, data_version: int) -> Snapshot:
        with self._lock:
            if data_version < 1 or data_version > len(self._snapshots):
                raise ApiError(DATA_VERSION_NOT_FOUND,
                               f"data version {data_version} not found", status=404)
            return self._snapshots[data_version - 1]

    def snapshot_as_of(self, ts: float) -> Snapshot:
        with self._lock:
            if not self._snapshots:
                raise ApiError(INVALID_AS_OF,
                               "no data imported before the requested time",
                               status=404)
            times = [s.created_at for s in self._snapshots]
            idx = bisect.bisect_right(times, ts) - 1
            if idx < 0:
                raise ApiError(INVALID_AS_OF,
                               "no data imported before the requested time",
                               status=404)
            return self._snapshots[idx]

    def current_ruleset(self) -> RuleSet:
        with self._lock:
            if self._current_ruleset_version == 0:
                return RuleSet(version=0, rules=(), created_at=0.0, note="empty")
            return self._rulesets[self._current_ruleset_version - 1]

    def ruleset_at_version(self, version: int) -> RuleSet:
        with self._lock:
            if version < 1 or version > len(self._rulesets):
                raise ApiError(RULE_VERSION_NOT_FOUND,
                               f"rule version {version} not found", status=404)
            return self._rulesets[version - 1]

    def ruleset_as_of(self, ts: float) -> RuleSet:
        """The rule set that was *active* at time ``ts``.

        Rule versions are appended monotonically and the active pointer only
        ever moves to the newest version, so the active set at ``ts`` is the
        latest version created at or before ``ts``.
        """
        with self._lock:
            if not self._rulesets:
                raise ApiError(INVALID_AS_OF,
                               "no rules published before the requested time",
                               status=404)
            times = [r.created_at for r in self._rulesets]
            idx = bisect.bisect_right(times, ts) - 1
            if idx < 0:
                raise ApiError(INVALID_AS_OF,
                               "no rules published before the requested time",
                               status=404)
            return self._rulesets[idx]

    def list_rule_versions(self) -> list[RuleSet]:
        with self._lock:
            return list(self._rulesets)

    def active_ruleset_version(self) -> int:
        with self._lock:
            return self._current_ruleset_version
