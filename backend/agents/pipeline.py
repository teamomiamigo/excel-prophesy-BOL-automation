"""
Pure orchestration for the AI agent pipeline: classify every eligible pending
record and draft reasoning for anything that isn't a clean approve.

No I/O here — no DB/mock-state writes, no invoice discovery, no email. Those
stay in main.py's POST /api/agents/run, matching how every other route in
this app keeps persistence/side-effects at the route layer and logic in
plain functions underneath it.
"""

from backend.agents.classifier import classify_record
from backend.agents.llm import draft_reason, template_reason


def run_agent_pipeline(pending_records: list[dict]) -> dict:
    """
    Args:
        pending_records: BOLSummary-shaped dicts (mock _mock_state values or
            BOLRecord rows already converted to dicts) for every currently
            pending record — not just ones from invoices just processed.

    Returns:
        {
            "proposals": [
                {
                    "bol_record_id": ..., "recommended_action": ...,
                    "confidence": ..., "reasoning": ..., "reasoning_source": ...,
                    "signal_summary": {...},
                    "invoice_number": ..., "technique_trip": ..., "manifest": ...,
                    "amount": ..., "cost_pct": ...,
                },
                ...
            ],
            "skipped_count": int,  # records with no cost data yet — no proposal drafted
        }
    """
    proposals = []
    skipped_count = 0

    for bol in pending_records:
        classification = classify_record(bol)
        action = classification["action"]

        if action is None:
            skipped_count += 1
            continue

        if action == "approve":
            # No LLM call for the boring case — template only, saves cost/latency.
            reasoning = template_reason(classification["reason_code"], classification["signals"])
            source = "template"
        else:
            reasoning, source = draft_reason(bol, classification)

        proposals.append({
            "bol_record_id": bol["id"],
            "recommended_action": action,
            "confidence": classification["confidence"],
            "reasoning": reasoning,
            "reasoning_source": source,
            "signal_summary": classification["signals"],
            "invoice_number": bol.get("invoice_number"),
            "technique_trip": bol.get("technique_trip"),
            "manifest": bol.get("manifest"),
            "amount": bol.get("amount"),
            "cost_pct": bol.get("cost_pct"),
        })

    return {"proposals": proposals, "skipped_count": skipped_count}
