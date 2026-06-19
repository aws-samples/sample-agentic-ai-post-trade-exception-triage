from __future__ import annotations

from typing import Any

from src.agentcore_runtime.tools.gateway_client import get_gateway_client
from src.gateway_tools.service import READ_ONLY_TOOLS
from src.common.triage_rules import infer_root_cause


def run_evidence_stage(case: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    client = get_gateway_client()
    # Tool authorization is case-scoped. Use the deterministic taxonomy for
    # Gateway requests instead of trusting free-form model summary output.
    root_cause = infer_root_cause(case)
    evidence: dict[str, Any] = {
        "trade_details": client.get_trade_details(case["exception_id"]),
        "settlement_status": client.get_settlement_status(case["exception_id"]),
    }
    if "allocation" in case.get("allowed_tool_scope", []):
        evidence["allocation_status"] = client.get_allocation_status(case["exception_id"])
    if "ssi" in case.get("allowed_tool_scope", []):
        evidence["ssi_record"] = client.get_ssi_record(
            case["exception_id"],
            case["counterparty_id"],
            case.get("account_id"),
        )
    if "prior_case" in case.get("allowed_tool_scope", []):
        prior = client.search_prior_cases(case["exception_id"], root_cause)
        evidence["prior_case"] = prior[0] if prior else None
    evidence = {key: value for key, value in evidence.items() if value}
    policy_decisions = [
        {
            "tool": tool,
            "decision": "ALLOW",
            "reason": "Read-only tool within synthetic case scope",
        }
        for tool in READ_ONLY_TOOLS
        if _tool_used(tool, evidence)
    ]
    if case["counterparty_id"].startswith("CP-SYN-RESTRICTED"):
        policy_decisions.append(
            {"tool": "get_ssi_record", "decision": "DENY", "reason": "Restricted counterparty marker"}
        )
    return {
        "evidence": evidence,
        "policy_decisions": policy_decisions,
        "trace": {"stage": "evidence", "framework": "strands-agents", "gateway": client.mode},
    }


def _tool_used(tool: str, evidence: dict[str, Any]) -> bool:
    return {
        "get_trade_details": "trade_details",
        "get_settlement_status": "settlement_status",
        "get_allocation_status": "allocation_status",
        "get_ssi_record": "ssi_record",
        "search_prior_cases": "prior_case",
        "get_playbook": "playbook",
    }[tool] in evidence
