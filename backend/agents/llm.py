"""
LLM-drafted reasoning for AI agent proposals.

Only called for non-"approve" recommendations (see backend/agents/pipeline.py) —
a clean approve gets a deterministic template only, no API call, no cost, no
latency for the boring case.

Never raises: any failure (no key configured, timeout, API error, empty
response) falls back to the same deterministic template a missing key would
produce — identical soft-fail shape to email_service.send_bol_export_email()'s
existing "SMTP not configured" fallback.
"""

import logging

from backend.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are drafting a short, plain-English reason for a freight-invoice "
    "reconciliation reviewer at SG360. The recommendation has already been "
    "made by a deterministic rule — you are NOT deciding it, only explaining "
    "it in one or two sentences a reviewer can read in under five seconds. "
    "Never invent a number that isn't given to you. Do not restate the "
    "recommended action itself, just the reasoning behind it."
)

# One template per classifier reason_code (backend/agents/classifier.py) — the
# no-API-key/failure fallback, and also the *only* reasoning used for anything
# the LLM wasn't even asked about (clean approves).
_TEMPLATES = {
    "cost_variance_high": (
        "ALG invoiced {deviation_pct:.1f}% away from our calculated Access "
        "Program rate (cost % {cost_pct_display}) — variance exceeds the 6% "
        "flag threshold."
    ),
    "uncertain_cost_basis": (
        "The calculated cost itself is flagged uncertain on this record "
        "(a rate, weight, or minimum-charge fallback was used), so cost % "
        "isn't reliable enough to auto-approve even though it may look close."
    ),
    "ambiguous_trip": (
        "This trip has more than one manifest in Technique and no BOL has "
        "been created yet — quantities are provisional until the correct "
        "manifest is confirmed."
    ),
    "quantity_mismatch": (
        "Weight, pallet, or piece counts differ from ALG's invoice by more "
        "than the normal tolerance — worth a manual check before approving."
    ),
    "cost_variance_moderate": (
        "ALG invoiced {deviation_pct:.1f}% away from our calculated rate — "
        "inside the flag threshold but outside the clean auto-approve range."
    ),
    "clean_match": (
        "Cost % is within 3% of our calculated Access Program rate and no "
        "quantity or data-quality flags are set."
    ),
    "no_cost_data": "No ALG invoice amount received yet — nothing to evaluate.",
}


def template_reason(reason_code: str, signals: dict) -> str:
    """Deterministic reasoning text — no API call. Used directly for clean
    approves (pipeline.py never calls the LLM for those) and as the fallback
    for everything else when draft_reason() can't get a real LLM response."""
    template = _TEMPLATES.get(reason_code, "No further detail available.")
    cost_pct = signals.get("cost_pct")
    cost_pct_display = f"{cost_pct * 100:.2f}%" if cost_pct is not None else "N/A"
    try:
        return template.format(
            deviation_pct=signals.get("deviation_pct") or 0.0,
            cost_pct_display=cost_pct_display,
        )
    except (KeyError, ValueError):
        return template


def draft_reason(bol: dict, classification: dict) -> tuple[str, str]:
    """
    Returns (reasoning_text, source) where source is "llm" or "template".
    Never raises.
    """
    reason_code = classification.get("reason_code", "")
    signals = classification.get("signals", {})
    template_text = template_reason(reason_code, signals)

    if not settings.ANTHROPIC_API_KEY:
        return template_text, "template"

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        user_content = (
            f"Recommended action: {classification.get('action')}\n"
            f"Reason code: {reason_code}\n"
            f"Signals: {signals}\n"
            f"Record: invoice={bol.get('invoice_number')}, "
            f"trip={bol.get('technique_trip')}, manifest={bol.get('manifest')}\n\n"
            "Draft the one-to-two sentence reasoning."
        )
        response = client.with_options(timeout=8.0, max_retries=1).messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=200,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        if text:
            return text, "llm"
        logger.warning(
            "[AI AGENT] LLM returned empty content for %s, using template fallback",
            bol.get("invoice_number"),
        )
        return template_text, "template"
    except Exception as exc:
        logger.warning(
            "[AI AGENT] LLM call failed for %s, using template fallback: %s",
            bol.get("invoice_number"), exc,
        )
        return template_text, "template"
