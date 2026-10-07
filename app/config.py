"""Capacity limits for trial runs (dry-run).

Hard limits reject a request up-front with a stable, specific error code so a
caller never waits on an oversized computation. The coverage limit bounds how
many transactions one report computes; exceeding it produces a *truncated*
result whose coverage is explicitly annotated (never silent under-computation).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    max_candidate_rules: int
    max_tx_scanned: int      # hard cap on tx the scope may cover
    max_tx_reported: int     # soft cap: report truncates beyond this
    max_diff_entries: int    # cap per diff list (new/lost/changed)

    @classmethod
    def from_env(cls) -> Limits:
        def get(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw is not None else default

        return cls(
            max_candidate_rules=get("RISK_MAX_CANDIDATE_RULES", 200),
            max_tx_scanned=get("RISK_MAX_TX_SCANNED", 1_000_000),
            max_tx_reported=get("RISK_MAX_TX_REPORTED", 100_000),
            max_diff_entries=get("RISK_MAX_DIFF_ENTRIES", 10_000),
        )
