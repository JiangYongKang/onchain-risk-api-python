"""In-memory, thread-safe, append-only storage.

Concurrency model
-----------------
A single re-entrant lock guards every mutation and every consistent read of the
published state. All stored facts (import batches, rule versions) are immutable;
a "version" is just a prefix of the append-only logs. Evaluations and dry-runs
copy that immutable prefix out under the lock and then evaluate lock-free, so a
long trial run never blocks (and is never affected by) concurrent imports or
rule publications.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.errors import ConflictError, NotFoundError
from app.models import DataVersion, ImportBatch, Rule, RuleSetVersion


def default_clock() -> int:
    return time.time_ns() // 1000


class Store:
    def __init__(self, clock: Callable[[], int] | None = None) -> None:
        self._clock = clock or default_clock
        self._lock = threading.RLock()

        # Append-only import log, kept in (imported_at, seq) order.
        self._batches: dict[str, ImportBatch] = {}
        self._order: list[str] = []
        self._seq: int = 0

        # Append-only rule-version chain.
        self._versions: list[RuleSetVersion] = []

    # ------------------------------------------------------------------ #
    # Lock helper
    # ------------------------------------------------------------------ #
    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._lock:
            yield

    def now_us(self) -> int:
        return self._clock()

    # ------------------------------------------------------------------ #
    # Imports / data versions
    # ------------------------------------------------------------------ #
    def import_batch(self, batch: ImportBatch) -> tuple[bool, int]:
        """Import a batch idempotently.

        Returns ``(deduplicated, data_version)``. A repeated ``batch_id`` with
        identical content is a no-op (deduplicated=True); different content for
        the same id is a conflict.
        """
        with self._lock:
            existing = self._batches.get(batch.batch_id)
            if existing is not None:
                # Idempotent retries compare business content only; the first
                # import's timestamp is authoritative and kept.
                same = (
                    existing.transactions == batch.transactions
                    and existing.edges == batch.edges
                )
                if same:
                    return True, self.data_version_count()
                raise ConflictError(
                    "batch_id already used with different content",
                    details={"batch_id": batch.batch_id},
                )
            self._batches[batch.batch_id] = batch
            self._order.append(batch.batch_id)
            self._seq += 1
            return False, self.data_version_count()

    def data_version_count(self) -> int:
        return len(self._order)

    def batches_prefix(self, n: int) -> list[ImportBatch]:
        """Return the first ``n`` batches (immutable data version dN)."""
        with self._lock:
            if n < 0 or n > len(self._order):
                raise NotFoundError(
                    "data version not found", details={"data_version": n}
                )
            return [self._batches[bid] for bid in self._order[:n]]

    def resolve_data_version_as_of(self, as_of: int) -> DataVersion:
        with self._lock:
            ids = [
                bid
                for bid in self._order
                if self._batches[bid].imported_at <= as_of
            ]
            return DataVersion(
                version=len(ids),
                as_of=as_of,
                batches=tuple(ids),
            )

    def current_data_version(self) -> DataVersion:
        with self._lock:
            return DataVersion(
                version=len(self._order),
                as_of=self._clock(),
                batches=tuple(self._order),
            )

    def get_data_version(self, n: int) -> DataVersion:
        with self._lock:
            if n < 1 or n > len(self._order):
                raise NotFoundError(
                    "data version not found", details={"data_version": n}
                )
            ids = tuple(self._order[:n])
            as_of = max(self._batches[bid].imported_at for bid in ids)
            return DataVersion(version=n, as_of=as_of, batches=ids)

    # ------------------------------------------------------------------ #
    # Rule versions
    # ------------------------------------------------------------------ #
    def publish_rules(
        self,
        rules: tuple[Rule, ...],
        note: str = "",
        parent_version: int | None = None,
        created_at: int | None = None,
    ) -> RuleSetVersion:
        with self._lock:
            version = len(self._versions) + 1
            rs = RuleSetVersion(
                version=version,
                rules=tuple(rules),
                created_at=created_at if created_at is not None else self._clock(),
                note=note,
                parent_version=parent_version,
            )
            self._versions.append(rs)
            return rs

    def rule_version_count(self) -> int:
        with self._lock:
            return len(self._versions)

    def get_rule_version(self, n: int) -> RuleSetVersion:
        with self._lock:
            if n < 1 or n > len(self._versions):
                raise NotFoundError(
                    "rule version not found", details={"rule_version": n}
                )
            return self._versions[n - 1]

    def current_rule_version(self) -> RuleSetVersion | None:
        with self._lock:
            return self._versions[-1] if self._versions else None

    def resolve_rule_version_as_of(self, as_of: int) -> RuleSetVersion | None:
        """Latest rule version published at or before ``as_of``."""
        with self._lock:
            found = [v for v in self._versions if v.created_at <= as_of]
            return found[-1] if found else None

    def rollback_to(self, n: int, note: str = "") -> RuleSetVersion:
        """Republish the exact rules of version ``n`` as a new version.

        The chain stays append-only/auditable; callers now see the old rule set
        again via the newest version.
        """
        with self._lock:
            target = self.get_rule_version(n)
            return self.publish_rules(
                target.rules, note=note or f"rollback to v{n}", parent_version=n
            )
