"""Rule trial run (dry-run): candidate rules vs a baseline on pinned snapshots.

A dry-run never writes anything. It:

1. Resolves *one* data snapshot per request (current / historical prefix /
   explicit data version), and *one* baseline rule set (current / historical
   instant / explicit rule version). The two are independent pins, but each
   evaluation side always pairs an immutable data prefix with an immutable
   rule version — versions are never mixed within one side.
2. Validates candidate rules *without* changing the published chain.
3. Evaluates both sides over the *same* locked snapshot, lock-free after the
   snapshot is taken (so concurrent imports/publications/queries continue on the
   current published version unaffected, and they cannot change mid-trial).
4. Produces a deterministic, rule-attributed diff: every added/removed hit and
   every change says which candidate/baseline rule contributed it.
5. Enforces capacity limits up-front and annotates partial coverage instead of
   silently under-computing or leaking internal errors to the caller.

Reproducibility guarantee
-------------------------
Publishing the candidate rules and querying with the *same data version* uses
the same engine on the same transaction prefix and the same rule content, so
trial results equal post-publish results exactly.
"""

from __future__ import annotations

from app.config import Limits
from app.engine import evaluate, validate_rules
from app.errors import LimitExceededError
from app.models import (
    BaselineSpecKind,
    DataScope,
    DiffKind,
    DryRunRequest,
    DryRunResponse,
    HitContrib,
    Rule,
    TxDiff,
    TxResult,
    TxView,
)
from app.service import RiskService


class CandidateRulesLimitExceeded(LimitExceededError):
    code = "DRYRUN_RULES_LIMIT_EXCEEDED"


class ScopeLimitExceeded(LimitExceededError):
    code = "DRYRUN_SCOPE_TOO_LARGE"


class ReportTruncated(LimitExceededError):
    """Raised only when a hard request asks for a report larger than cap."""

    code = "DRYRUN_REPORT_LIMIT_EXCEEDED"


def _view(res: TxResult | None) -> TxView | None:
    if res is None:
        return None
    return TxView(
        tx_id=res.tx_id,
        frm=res.frm,
        to=res.to,
        amount=res.amount,
        score=res.score,
        tags=list(res.tags),
        # Deterministic ordering for "completely equal" assertions.
        hit_rule_ids=[h.rule_id for h in res.hits],
        risk=res.risk,
    )


def _contributors(res: TxResult | None) -> list[HitContrib]:
    if res is None:
        return []
    return [
        HitContrib(rule_id=h.rule_id, score=h.score, tags=list(h.tags))
        for h in res.hits
    ]


def _partial_reason(truncated: bool, has_missing: bool) -> str | None:
    """Stable, distinguishable reason code for partial/incomplete coverage."""
    if truncated and has_missing:
        return "truncated_and_missing_tx"
    if truncated:
        return "truncated"
    if has_missing:
        return "missing_tx"
    return None


def _sort_diffs(
    diffs: list[TxDiff], cap: int
) -> tuple[list[TxDiff], bool, int]:
    diffs.sort(key=lambda d: d.tx_id)
    total = len(diffs)
    if total > cap:
        return diffs[:cap], True, total
    return diffs, False, total


def run_dry_run(
    svc: RiskService,
    request: DryRunRequest,
    limits: Limits | None = None,
    allow_truncation: bool = True,
) -> DryRunResponse:
    limits = limits or Limits.from_env()
    warnings: list[str] = []

    # 1) Candidate validation — no store mutation.
    candidates = tuple(Rule.from_in(r) for r in request.candidate_rules)
    validate_rules(candidates)
    if len(candidates) > limits.max_candidate_rules:
        raise CandidateRulesLimitExceeded(
            "too many candidate rules",
            details={
                "submitted": len(candidates),
                "limit": limits.max_candidate_rules,
            },
        )

    # 2) Resolve one immutable data snapshot (does not take a write lock long).
    #
    # If the baseline is a historical instant and the caller did not pin a data
    # scope explicitly, the data is automatically pinned to that same instant,
    # so candidate and baseline answers are both self-consistent with that
    # point in time (today's data is never silently mixed with old rules).
    scope = request.scope
    default_scope = DataScope()
    baseline_as_of = (
        request.baseline.as_of
        if request.baseline.kind is BaselineSpecKind.AS_OF
        else None
    )
    if baseline_as_of is not None and scope == default_scope:
        scope = DataScope(kind="as_of", as_of=baseline_as_of)
        warnings.append(
            "data scope auto-pinned to baseline as_of for historical consistency"
        )
    snap = svc.load_snapshot(scope)
    total = snap.total_count

    # 3) Coverage bookkeeping (missing requested ids are never silent).
    missing: list[str] = []
    if snap.tx_filter is not None:
        missing = sorted(snap.tx_filter - snap.all_tx_ids)
        if missing:
            warnings.append(
                "some requested tx_ids are absent from the pinned data version"
            )

    in_scope = sorted(t.tx_id for t in snap.transactions)
    if len(in_scope) > limits.max_tx_scanned:
        raise ScopeLimitExceeded(
            "data scope exceeds scan limit",
            details={"in_scope": len(in_scope), "limit": limits.max_tx_scanned},
        )

    # 4) Report truncation: compute only the first N (deterministic order).
    truncated = False
    if len(in_scope) > limits.max_tx_reported:
        if not allow_truncation:
            raise ReportTruncated(
                "result size exceeds report limit and truncation disabled",
                details={
                    "in_scope": len(in_scope),
                    "limit": limits.max_tx_reported,
                },
            )
        covered_set = frozenset(in_scope[: limits.max_tx_reported])
        covered_tx = [t for t in snap.transactions if t.tx_id in covered_set]
        truncated = True
        warnings.append(
            f"result truncated to the first {limits.max_tx_reported} "
            "transactions in deterministic (ts, tx_id) order"
        )
    else:
        covered_tx = snap.transactions

    # 5) Baseline resolution.
    b = request.baseline
    base_rules, base_ver = svc.resolve_baseline_rules(
        kind=b.kind.value, rule_version=b.rule_version, as_of=b.as_of
    )

    # 6) Evaluate both sides on the SAME snapshot — no locks held during eval.
    cand_res = evaluate(candidates, covered_tx, snap.graph)
    base_res = evaluate(base_rules, covered_tx, snap.graph)

    # 7) Diff.
    new_hits: list[TxDiff] = []
    lost_hits: list[TxDiff] = []
    changed: list[TxDiff] = []
    unchanged = 0
    for tx in sorted(covered_tx, key=lambda t: (t.ts, t.tx_id)):
        c = cand_res[tx.tx_id]
        bl = base_res[tx.tx_id]
        if not c.risk and not bl.risk:
            unchanged += 1
            continue
        kind: DiffKind
        if c.risk and not bl.risk:
            kind = DiffKind.NEW_HIT
            target = new_hits
        elif bl.risk and not c.risk:
            kind = DiffKind.LOST_HIT
            target = lost_hits
        else:
            if c.score != bl.score:
                kind = DiffKind.SCORE_CHANGED
            elif set(c.tags) != set(bl.tags):
                kind = DiffKind.TAGS_CHANGED
            elif {h.rule_id for h in c.hits} != {h.rule_id for h in bl.hits}:
                kind = DiffKind.HIT_RULES_CHANGED
            else:
                unchanged += 1
                continue
            target = changed

        cand_tags = set(c.tags)
        base_tags = set(bl.tags)
        target.append(
            TxDiff(
                tx_id=tx.tx_id,
                kind=kind,
                baseline=_view(bl),
                candidate=_view(c),
                candidate_contributors=_contributors(c),
                baseline_contributors=_contributors(bl),
                added_tags=sorted(cand_tags - base_tags),
                removed_tags=sorted(base_tags - cand_tags),
                score_delta=c.score - bl.score,
            )
        )

    new_hits, t1, new_total = _sort_diffs(new_hits, limits.max_diff_entries)
    lost_hits, t2, lost_total = _sort_diffs(lost_hits, limits.max_diff_entries)
    changed, t3, changed_total = _sort_diffs(changed, limits.max_diff_entries)
    if t1 or t2 or t3:
        truncated = True
        warnings.append(
            f"diff list(s) truncated to {limits.max_diff_entries} entries each; "
            "see new_hits_count/lost_hits_count/changed_count for totals"
        )

    return DryRunResponse(
        data_version=snap.version,
        candidate_rule_count=len(candidates),
        baseline_rule_version=base_ver,
        total_tx_count=total,
        in_scope_tx_count=len(in_scope),
        compared_tx_count=len(in_scope),
        covered_tx_count=len(covered_tx),
        covered=[t.tx_id for t in sorted(covered_tx, key=lambda t: (t.ts, t.tx_id))],
        missing_tx_ids=missing,
        complete=not truncated and not missing,
        partial_reason=_partial_reason(truncated, bool(missing)),
        truncated=truncated,
        new_hits=new_hits,
        lost_hits=lost_hits,
        changed=changed,
        new_hits_count=new_total,
        lost_hits_count=lost_total,
        changed_count=changed_total,
        unchanged_count=unchanged,
        warnings=warnings,
    )
