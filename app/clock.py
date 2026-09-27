"""Injectable clock so tests can pin time for deterministic historical replay."""
from __future__ import annotations

import time
from collections.abc import Callable


class Clock:
    """Returns current time in epoch milliseconds.

    A fixed time (or callable) can be injected in tests; by default the
    real wall clock is used.
    """

    def __init__(self, fixed_ms: int | None = None,
                 fn: Callable[[], int] | None = None):
        self._fixed_ms = fixed_ms
        self._fn = fn

    def now_ms(self) -> int:
        if self._fn is not None:
            return int(self._fn())
        if self._fixed_ms is not None:
            return int(self._fixed_ms)
        return int(time.time() * 1000)
