"""Logging configuration.

Logs always carry the batch id, data/rule versions and, for risk queries,
each rule's decision basis. One JSON-ish line per event, prefixed with a
tag so it is easy to grep.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("risk.api")


def configure_logging(level: int = logging.INFO):
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.setLevel(level)


def log_event(event: str, **fields):
    """Emit a stable, greppable event line with key fields."""
    payload = {"event": event, **fields}
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           default=str))
    return payload
