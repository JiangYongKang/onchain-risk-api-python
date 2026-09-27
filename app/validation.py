"""Manual, stable-coded validation of raw import payloads.

Returning per-item results (instead of letting FastAPI/pydantic reject the
whole request) lets a batch partially succeed and gives every bad row a
stable, distinguishable rejection code.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from . import limits
from .errors import (
    ADDRESS_INVALID,
    AMOUNT_NOT_NUMBER,
    AMOUNT_NOT_POSITIVE,
    AMOUNT_TOO_LARGE,
    ITEM_CONFLICT_IN_BATCH,
    ITEM_DUPLICATE_IN_BATCH,
    ITEM_FIELD_MISSING,
    ITEM_NOT_OBJECT,
    RULE_INVALID,
    TS_INVALID,
    TS_OUT_OF_RANGE,
    TX_HASH_INVALID,
    ServiceError,
)
from .models import ItemResult, ItemStatus

RULE_KINDS = {"TX_AMOUNT", "TIME_WINDOW", "ADDRESS_FANOUT", "WATCHED_ADDRESS"}

_HASH_RE = re.compile(r"^0x[0-9a-f]{6,128}$")
_ADDR_RE = re.compile(r"^0x[0-9a-f]{6,128}$")


@dataclass(frozen=True)
class NormalizedTx:
    index: int
    tx_hash: str
    from_addr: str
    to_addr: str
    amount: float
    ts_ms: int


@dataclass(frozen=True)
class NormalizedRelation:
    index: int
    from_addr: str
    to_addr: str
    kind: str | None


@dataclass
class ValidatedTxs:
    accepted: list[NormalizedTx]          # unique valid rows, first occurrence
    items: list[ItemResult]               # one result per input row


def _reject(index: int, code: str, message: str, tx_hash: str | None = None) -> ItemResult:
    return ItemResult(index=index, tx_hash=tx_hash, status=ItemStatus.REJECTED,
                      error=code, message=message)


def _is_real_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _validate_addresses(item: dict, index: int) -> tuple[str | None, str | None, ItemResult | None]:
    for field in ("from_addr", "to_addr"):
        if field not in item or item[field] is None:
            return None, None, _reject(index, ITEM_FIELD_MISSING, f"field '{field}' is required")
        raw = item[field]
        if not isinstance(raw, str):
            return None, None, _reject(index, ADDRESS_INVALID, f"field '{field}' must be a string")
        norm = raw.strip().lower()
        if not _ADDR_RE.match(norm):
            return None, None, _reject(index, ADDRESS_INVALID, f"field '{field}' is not a valid address")
        if field == "from_addr":
            frm = norm
        else:
            to = norm
    return frm, to, None


def _validate_amount(item: dict, index: int) -> tuple[float | None, ItemResult | None]:
    if "amount" not in item or item["amount"] is None:
        return None, _reject(index, ITEM_FIELD_MISSING, "field 'amount' is required")
    v = item["amount"]
    if not _is_real_number(v):
        return None, _reject(index, AMOUNT_NOT_NUMBER, "amount must be a finite number")
    if v <= 0:
        return None, _reject(index, AMOUNT_NOT_POSITIVE, "amount must be > 0")
    if v > limits.MAX_AMOUNT:
        return None, _reject(index, AMOUNT_TOO_LARGE, f"amount exceeds {limits.MAX_AMOUNT:g}")
    return float(v), None


def _validate_ts(item: dict, index: int, now_ms: int) -> tuple[int | None, ItemResult | None]:
    if "ts_ms" not in item or item["ts_ms"] is None:
        return None, _reject(index, ITEM_FIELD_MISSING, "field 'ts_ms' is required")
    v = item["ts_ms"]
    if not isinstance(v, int) or isinstance(v, bool):
        return None, _reject(index, TS_INVALID, "ts_ms must be an integer epoch in milliseconds")
    if v < now_ms - limits.PAST_WINDOW_MS or v > now_ms + limits.FUTURE_SKEW_MS:
        return None, _reject(index, TS_OUT_OF_RANGE,
                             "ts_ms is outside the accepted past/future window")
    return v, None


def validate_transactions(raw: Any, now_ms: int) -> ValidatedTxs:
    """Validate a raw ``transactions`` list (no DB access here)."""
    if not isinstance(raw, list):
        raise ServiceError("BATCH_EMPTY", "field 'transactions' must be a non-empty list",
                           status_code=422)
    items: list[ItemResult] = []
    accepted: list[NormalizedTx] = []
    seen: dict[str, NormalizedTx] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            items.append(_reject(index, ITEM_NOT_OBJECT, "transaction must be an object"))
            continue
        if "tx_hash" not in item or item["tx_hash"] is None:
            items.append(_reject(index, ITEM_FIELD_MISSING, "field 'tx_hash' is required"))
            continue
        raw_hash = item["tx_hash"]
        if not isinstance(raw_hash, str):
            items.append(_reject(index, TX_HASH_INVALID, "tx_hash must be a string"))
            continue
        h = raw_hash.strip().lower()
        if not _HASH_RE.match(h):
            items.append(_reject(index, TX_HASH_INVALID, "tx_hash must be 0x-prefixed hex"))
            continue
        frm, to, err = _validate_addresses(item, index)
        if err is not None:
            items.append(err)
            continue
        amount, err = _validate_amount(item, index)
        if err is not None:
            items.append(err)
            continue
        ts, err = _validate_ts(item, index, now_ms)
        if err is not None:
            items.append(err)
            continue

        if h in seen:
            prev = seen[h]
            same = (prev.from_addr == frm and prev.to_addr == to
                    and prev.amount == amount and prev.ts_ms == ts)
            if same:
                items.append(ItemResult(
                    index=index, tx_hash=h, status=ItemStatus.DUPLICATE,
                    error=ITEM_DUPLICATE_IN_BATCH,
                    message="duplicate of an earlier row in the same batch; counted once"))
            else:
                items.append(_reject(index, ITEM_CONFLICT_IN_BATCH,
                                     "same tx_hash appears with conflicting fields in the batch",
                                     tx_hash=h))
            continue
        norm = NormalizedTx(index, h, frm, to, amount, ts)
        seen[h] = norm
        accepted.append(norm)
        items.append(ItemResult(index=index, tx_hash=h, status=ItemStatus.ACCEPTED))
    return ValidatedTxs(accepted=accepted, items=items)


def validate_relations(raw: Any) -> tuple[list[NormalizedRelation], list[ItemResult]]:
    if not isinstance(raw, list):
        raise ServiceError("RELATION_BATCH_EMPTY",
                           "field 'relations' must be a list", status_code=422)
    accepted: list[NormalizedRelation] = []
    errors: list[ItemResult] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(_reject(index, ITEM_NOT_OBJECT, "relation must be an object"))
            continue
        frm, to, err = _validate_addresses(item, index)
        if err is not None:
            errors.append(err)
            continue
        kind = item.get("relation")
        if kind is not None and not (isinstance(kind, str) and kind.strip()):
            errors.append(_reject(index, "RELATION_INVALID",
                                  "field 'relation' must be a non-empty string when present"))
            continue
        kind = kind.strip() if isinstance(kind, str) else None
        edge = (frm, to)
        if edge in seen:
            continue  # idempotent duplicate edge inside the same import
        seen.add(edge)
        accepted.append(NormalizedRelation(index, frm, to, kind))
    return accepted, errors


def _checked_addr(v: Any) -> str:
    if not isinstance(v, str) or not _ADDR_RE.match(v.strip().lower()):
        raise ServiceError(RULE_INVALID, "watchlist contains an invalid address")
    return v.strip().lower()


def validate_rule(raw: dict) -> dict:
    """Validate a rule payload and return normalized params."""
    if not isinstance(raw, dict):
        raise ServiceError(RULE_INVALID, "rule body must be an object")
    rid = raw.get("rule_id")
    if not isinstance(rid, str) or not rid.strip():
        raise ServiceError(RULE_INVALID, "rule_id is required")
    rid = rid.strip()
    kind = raw.get("kind")
    if kind not in RULE_KINDS:
        raise ServiceError(RULE_INVALID,
                           f"kind must be one of {sorted(RULE_KINDS)}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ServiceError(RULE_INVALID, "name is required")
    score = raw.get("score")
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        raise ServiceError(RULE_INVALID, "score must be an integer in [0,100]")
    tag = raw.get("tag")
    if not isinstance(tag, str) or not tag.strip():
        raise ServiceError(RULE_INVALID, "tag is required")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise ServiceError(RULE_INVALID, "params must be an object")

    eff = raw.get("effective_at")
    if eff is not None and (not isinstance(eff, int) or isinstance(eff, bool) or eff <= 0):
        raise ServiceError(RULE_INVALID, "effective_at must be a positive epoch-ms integer")

    if kind == "TX_AMOUNT":
        m = params.get("min_amount")
        if not _is_real_number(m) or m <= 0:
            raise ServiceError(RULE_INVALID, "params.min_amount must be a positive number")
        params = {"min_amount": float(m)}
    elif kind == "TIME_WINDOW":
        w = params.get("window_ms")
        c = params.get("max_count")
        if not isinstance(w, int) or isinstance(w, bool) or w <= 0:
            raise ServiceError(RULE_INVALID, "params.window_ms must be a positive integer")
        if not isinstance(c, int) or isinstance(c, bool) or c < 1:
            raise ServiceError(RULE_INVALID, "params.max_count must be an integer >= 1")
        params = {"window_ms": w, "max_count": c}
    elif kind == "ADDRESS_FANOUT":
        d = params.get("direction", "out")
        if d not in ("out", "in"):
            raise ServiceError(RULE_INVALID, "params.direction must be 'out' or 'in'")
        c = params.get("min_count")
        w = params.get("within_ms")
        if not isinstance(c, int) or isinstance(c, bool) or c < 1:
            raise ServiceError(RULE_INVALID, "params.min_count must be an integer >= 1")
        if not isinstance(w, int) or isinstance(w, bool) or w <= 0:
            raise ServiceError(RULE_INVALID, "params.within_ms must be a positive integer")
        params = {"direction": d, "min_count": c, "within_ms": w}
    elif kind == "WATCHED_ADDRESS":
        addrs = params.get("addresses")
        if not isinstance(addrs, list) or not addrs:
            raise ServiceError(RULE_INVALID, "params.addresses must be a non-empty list")
        match = params.get("match", "either")
        if match not in ("from", "to", "either"):
            raise ServiceError(RULE_INVALID, "params.match must be 'from','to' or 'either'")
        params = {"addresses": [_checked_addr(a) for a in addrs], "match": match}

    return {
        "rule_id": rid,
        "kind": kind,
        "name": name.strip(),
        "description": str(raw.get("description") or ""),
        "params": params,
        "score": score,
        "tag": tag.strip(),
        "effective_at": eff,
    }
