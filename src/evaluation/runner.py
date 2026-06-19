from __future__ import annotations

import json
import os
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agentcore_runtime.triage_workflow import run_triage
from src.common.data_store import DATA_DIR, decimal_safe, get_data_store
from src.common import config
from src.common.triage_rules import normalize_exception, policy_confidence_gate, scope_and_severity, validate_recommendation
from src.gateway_tools.service import READ_ONLY_TOOLS


TOOL_SCOPE = {
    "get_trade_details": "trade",
    "get_settlement_status": "settlement",
    "get_allocation_status": "allocation",
    "get_ssi_record": "ssi",
    "search_prior_cases": "prior_case",
    "get_playbook": "playbook",
}


def _invoke_deployed_runtime(agent_runtime_arn: str, case: dict[str, Any]) -> dict[str, Any]:
    """Invoke the deployed AgentCore Runtime for a single case.

    Uses the same parse-and-retry helper as the task Lambda to keep parity.
    """
    from src.lambda_tasks.handlers import _invoke_agentcore_runtime

    return _invoke_agentcore_runtime(agent_runtime_arn, case)


def run_evaluation() -> dict[str, Any]:
    agent_runtime_arn = os.environ.get("AGENT_RUNTIME_ARN", "")
    invocation_mode = "DEPLOYED_RUNTIME" if agent_runtime_arn else "IN_PROCESS"
    store = get_data_store()
    cases = json.loads((DATA_DIR / "golden_dataset.json").read_text(encoding="utf-8"))
    results = []
    for golden in cases:
        case = normalize_exception(store.exception_by_case_key(golden["case_key"]))
        scope = scope_and_severity(case)
        if scope["eligible"]:
            if agent_runtime_arn:
                agent = _invoke_deployed_runtime(agent_runtime_arn, case)
            else:
                agent = run_triage(case)
            validation = validate_recommendation(case, agent["recommendation"])
            gate = policy_confidence_gate(case, agent["recommendation"], validation)
            recommendation = agent["recommendation"]
            latency_ms = agent["trace"]["latency_ms"]
            policy_decisions = agent.get("policy_decisions", [])
        else:
            recommendation = {
                "root_cause_category": "POLICY_OR_CUTOFF_ESCALATION",
                "playbook_id": "PB-ESC-001",
                "recommended_queue": "URGENT_MANUAL_REVIEW",
                "evidence_refs": [],
                "confidence": 1.0,
            }
            validation = {"accepted": True}
            gate = {"decision": "ESCALATE"}
            latency_ms = 0
            policy_decisions = []
        evidence_refs = set(recommendation.get("evidence_refs", []))
        required_refs = set(golden["required_evidence_refs"])
        evidence_recall = 1.0 if not required_refs else len(evidence_refs & required_refs) / len(required_refs)
        expected_policy_denial = case["counterparty_id"].startswith(config.RESTRICTED_COUNTERPARTY_PREFIX)
        observed_policy_denial = any(
            "Restricted counterparty" in reason for reason in scope.get("eligibility_reasons", [])
        ) or gate.get("reason") == "Restricted counterparty"
        results.append(
            {
                "case_key": golden["case_key"],
                "playbook_match": recommendation.get("playbook_id") == golden["expected_playbook_id"],
                "evidence_recall": evidence_recall,
                "escalation_match": (gate["decision"] == "ESCALATE") == golden["expected_escalation"],
                "policy_denial_match": observed_policy_denial == expected_policy_denial,
                "unauthorized_tool_attempt": _has_unauthorized_tool_attempt(case, policy_decisions),
                "valid_output": validation["accepted"],
                "latency_ms": latency_ms,
                "recommendation": recommendation,
            }
        )
    metrics = {
        "evaluation_run_id": f"eval-{uuid.uuid4().hex[:10]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case_count": len(results),
        "agent_invocation_mode": invocation_mode,
        "playbook_accuracy": _mean(item["playbook_match"] for item in results),
        "evidence_recall": _mean(item["evidence_recall"] for item in results),
        "escalation_correctness": _mean(item["escalation_match"] for item in results),
        "policy_denial_correctness": _mean(item["policy_denial_match"] for item in results),
        "unauthorized_tool_attempt_rate": _mean(item["unauthorized_tool_attempt"] for item in results),
        "invalid_output_rate": 1.0 - _mean(item["valid_output"] for item in results),
        "latency_p50_ms": _percentile([item["latency_ms"] for item in results], 50),
        "latency_p95_ms": _percentile([item["latency_ms"] for item in results], 95),
        "agentcore_evaluations": "Configured in CDK with an AgentCore evaluator; this runner computes deterministic golden metrics.",
        "results": results,
    }
    _persist(metrics)
    return metrics


def _has_unauthorized_tool_attempt(case: dict[str, Any], policy_decisions: list[dict[str, Any]]) -> bool:
    allowed_scopes = set(case.get("allowed_tool_scope", []))
    for decision in policy_decisions:
        tool = decision.get("tool", "")
        if tool not in READ_ONLY_TOOLS:
            return True
        if TOOL_SCOPE.get(tool) not in allowed_scopes:
            return True
        if decision.get("decision") not in {"ALLOW", "DENY"}:
            return True
    return False


def _persist(metrics: dict[str, Any]) -> None:
    out_dir = Path(os.environ.get("EVALUATION_OUTPUT_DIR", "evaluation-output"))
    out_dir.mkdir(exist_ok=True)
    (out_dir / "latest-evaluation.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    table_name = os.environ.get("TABLE_NAME", "")
    if table_name:
        import boto3

        boto3.resource("dynamodb").Table(table_name).put_item(
            Item=decimal_safe({"PK": "EVALUATION#LATEST", "SK": "SUMMARY", "entity_type": "EVALUATION", "payload": metrics})
        )
    bucket = os.environ.get("ARTIFACT_BUCKET", "")
    if bucket:
        import boto3

        key = f"evaluations/{metrics['evaluation_run_id']}.json"
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=json.dumps(metrics, indent=2).encode("utf-8"))
        metrics["artifact_s3_uri"] = f"s3://{bucket}/{key}"


def _mean(values: Any) -> float:
    vals = [float(v) for v in values]
    return round(sum(vals) / max(len(vals), 1), 4)


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    if len(values) == 1:
        return values[0]
    return int(statistics.quantiles(values, n=100)[percentile - 1])


if __name__ == "__main__":
    print(json.dumps(run_evaluation(), indent=2))
