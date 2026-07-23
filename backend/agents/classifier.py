"""
Deterministic classifier for the AI agent pipeline.

No LLM involved here on purpose — this is a pure function of the same numbers
already rendered on the dashboard, using the exact same thresholds, so the
agent's recommendation can never disagree with what Katie already sees
color-coded on a record. The LLM (backend/agents/llm.py) only drafts the
human-readable *reasoning* for a decision this module has already made.

Thresholds mirror frontend/src/components/BOLRow.jsx (getCostPctStyle(),
QUANTITY_MISMATCH_THRESHOLD, isUnverifiedQuantity()) and the quantity-match
tolerance already used server-side for invoice matching
(main.py's _CLOSE_MATCH_THRESHOLD, same 0.15 value, different purpose).
If either dashboard threshold ever changes, update this module in the same
commit — there is no shared module between frontend and backend today.
"""

from decimal import Decimal
from typing import Optional

# Cost % deviation bands — identical to BOLRow.jsx::getCostPctStyle()
_GREEN_MAX_DEVIATION = 3.0
_ORANGE_MAX_DEVIATION = 6.0

# Quantity-mismatch tolerance — identical to BOLRow.jsx::QUANTITY_MISMATCH_THRESHOLD
_QUANTITY_MISMATCH_THRESHOLD = 0.15


def _rel_diff(diff_val, alg_val) -> float:
    """Mirrors BOLRow.jsx::_relDiff() exactly."""
    if diff_val is None or not alg_val:
        return 0.0
    return abs(float(diff_val)) / abs(float(alg_val))


def _quantity_mismatch_score(bol: dict) -> float:
    """Mirrors BOLRow.jsx::hasSevereQuantityMismatch()'s summed score."""
    return (
        _rel_diff(bol.get("weight_diff"), bol.get("alg_weight"))
        + _rel_diff(bol.get("pallet_diff"), bol.get("alg_pallets"))
        + _rel_diff(bol.get("pcs_diff"), bol.get("alg_pcs"))
    )


def classify_record(bol: dict) -> dict:
    """
    Returns:
        {
            "action": "approve" | "needs_review" | "flag" | None,
            "confidence": float,   # confidence in THIS recommendation, not in
                                    # the accuracy of cost_pct/access_prog itself
            "reason_code": str,
            "signals": dict,       # the numbers that drove the call — stored
                                    # verbatim as AgentProposal.signal_summary
        }

    action=None means there isn't enough data yet to judge (amount/cost_pct
    still null) — the caller should skip these, not manufacture a proposal
    from nothing.
    """
    cost_pct = bol.get("cost_pct")

    signals = {
        "cost_pct": float(cost_pct) if cost_pct is not None else None,
        "weight_diff": _decimal_to_float(bol.get("weight_diff")),
        "pallet_diff": bol.get("pallet_diff"),
        "pcs_diff": bol.get("pcs_diff"),
        "quantity_mismatch_score": None,
        "tariff_zone_approximate": bool(bol.get("tariff_zone_approximate")),
        "weight_source_fallback": bool(bol.get("weight_source_fallback")),
        "min_charge_uncertain": bool(bol.get("min_charge_uncertain")),
        "is_ambiguous_trip": bool(bol.get("is_ambiguous_trip")),
        "mismatch_acknowledged": bool(bol.get("mismatch_acknowledged")),
        "bol_number": bol.get("bol_number"),
    }

    if cost_pct is None:
        return {
            "action": None,
            "confidence": 0.0,
            "reason_code": "no_cost_data",
            "signals": signals,
        }

    deviation = abs(float(cost_pct) * 100 - 100)
    mismatch_score = _quantity_mismatch_score(bol)
    signals["deviation_pct"] = round(deviation, 2)
    signals["quantity_mismatch_score"] = round(mismatch_score, 4)

    uncertain_cost_basis = (
        bol.get("tariff_zone_approximate")
        or bol.get("weight_source_fallback")
        or bol.get("min_charge_uncertain")
    )
    ambiguous_trip = bool(bol.get("is_ambiguous_trip")) and not bol.get("bol_number")
    # Mirrors BOLRow.jsx::isUnverifiedQuantity() — once Katie's acknowledged a
    # severe mismatch (POST /api/bols/{id}/acknowledge-mismatch), the dashboard
    # stops badging it and the agent shouldn't re-flag what she's already cleared.
    severe_mismatch = (
        mismatch_score > _QUANTITY_MISMATCH_THRESHOLD
        and not bol.get("mismatch_acknowledged")
    )

    # Priority order below matches how these same signals already read on the
    # dashboard: a red cost_pct is the clearest, highest-priority problem;
    # everything else that keeps the record out of a clean auto-approve reads
    # as "needs a human look," same amber tier the ~EST/~UNVERIFIED badges use.
    if deviation >= _ORANGE_MAX_DEVIATION:
        return {
            "action": "flag",
            "confidence": 0.85,
            "reason_code": "cost_variance_high",
            "signals": signals,
        }

    if uncertain_cost_basis:
        return {
            "action": "needs_review",
            "confidence": 0.5,
            "reason_code": "uncertain_cost_basis",
            "signals": signals,
        }

    if ambiguous_trip:
        return {
            "action": "needs_review",
            "confidence": 0.5,
            "reason_code": "ambiguous_trip",
            "signals": signals,
        }

    if severe_mismatch:
        return {
            "action": "needs_review",
            "confidence": 0.55,
            "reason_code": "quantity_mismatch",
            "signals": signals,
        }

    if deviation >= _GREEN_MAX_DEVIATION:
        return {
            "action": "needs_review",
            "confidence": 0.6,
            "reason_code": "cost_variance_moderate",
            "signals": signals,
        }

    return {
        "action": "approve",
        "confidence": 0.9,
        "reason_code": "clean_match",
        "signals": signals,
    }


def _decimal_to_float(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, Decimal):
        return float(val)
    return val
