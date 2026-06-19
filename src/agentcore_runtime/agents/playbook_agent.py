from __future__ import annotations

from typing import Any

from src.agentcore_runtime.tools.gateway_client import get_gateway_client
from src.common.triage_rules import ROOT_CAUSE_TO_PLAYBOOK, infer_root_cause


def run_playbook_stage(case: dict[str, Any], summary: dict[str, Any], evidence_package: dict[str, Any]) -> dict[str, Any]:
    client = get_gateway_client()
    root_cause = infer_root_cause(case, evidence_package.get("evidence", {}))
    playbook_id = ROOT_CAUSE_TO_PLAYBOOK[root_cause]
    playbook = client.get_playbook(case["exception_id"], playbook_id)
    if not playbook:
        raise ValueError(f"Missing synthetic playbook {playbook_id}")
    evidence_package.setdefault("evidence", {})["playbook"] = playbook
    evidence_package.setdefault("policy_decisions", []).append(
        {
            "tool": "get_playbook",
            "decision": "ALLOW",
            "reason": "Read-only playbook tool within synthetic case scope",
        }
    )
    return {
        "root_cause_category": root_cause,
        "playbook": playbook,
        "playbook_match_reason": f"{case['exception_type']} maps to approved playbook {playbook_id}.",
        "trace": {"stage": "playbook", "framework": "strands-agents", "gateway": client.mode},
    }
