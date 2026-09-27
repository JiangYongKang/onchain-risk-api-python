"""Explicit, documented operational caps.

Violating a hard cap yields a stable rejection; soft caps produce a result
that is explicitly marked ``truncated`` with a reason — never silent
truncation.
"""
from __future__ import annotations

MAX_BATCH_ITEMS = 10_000          # hard: reject whole import batch
MAX_RELATION_BATCH = 5_000        # hard: reject whole relation batch
MAX_FANOUT = 200                  # soft: explore up to this many neighbours per node
MAX_GRAPH_NODES = 10_000          # soft: total nodes visited per traversal
MAX_GRAPH_DEPTH = 6               # soft: maximum traversal depth
MAX_AMOUNT = 1e18                 # hard: per-transaction amount cap
PAST_WINDOW_MS = 365 * 24 * 3600 * 1000  # hard: timestamps older than this -> reject
FUTURE_SKEW_MS = 300 * 1000       # hard: timestamps more than this in the future -> reject
