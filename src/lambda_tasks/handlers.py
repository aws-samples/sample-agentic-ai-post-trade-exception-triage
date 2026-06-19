from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from src.agentcore_runtime.triage_workflow import run_triage
from src.common.data_store import decimal_safe, get_data_store
from src.common.triage_rules import (
    normalize_exception,
    policy_confidence_gate,
    scope_and_severity,
    validate_recommendation,
)


class RuntimeInvocationError(RuntimeError):
    """Raised when the AgentCore Runtime returns a malformed or empty response."""


_RETRYABLE_ERROR_CODES = {"ThrottlingException", "TooManyRequestsException"}
_MAX_BACKOFF_SECONDS = 5.0
_MIN_BACKOFF_SECONDS = 0.1
_CASE_KEY_RE = re.compile(r"^[a-z0-9_]{1,80}$")
_EXECUTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def normalize_exception_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    store = get_data_store()
    raw = event.get("exception") or event
    if "case_key" in raw and "exception_id" not in raw:
        found = store.exception_by_case_key(raw["case_key"])
        if not found:
            raise ValueError(f"Unknown synthetic case_key {raw['case_key']}")
        raw = found
    normalized = normalize_exception(raw)
    inbound_break = (
        store.inbound_break_by_case_key(normalized["case_key"])
        or store.inbound_break_by_exception(normalized["exception_id"])
    )
    normalization = _build_normalization_trace(raw, normalized, inbound_break)
    return {**event, "case": normalized, "normalization": normalization}


def scope_and_severity_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    decision = scope_and_severity(event["case"])
    return {**event, "scope": decision}


def invoke_agentcore_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    agent_runtime_arn = os.environ.get("AGENT_RUNTIME_ARN", "")
    if agent_runtime_arn:
        response = _invoke_agentcore_runtime(agent_runtime_arn, event["case"])
    else:
        response = run_triage(event["case"])
    return {**event, "agent": response}


def validate_output_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    recommendation = event["agent"]["recommendation"]
    validation = validate_recommendation(event["case"], recommendation)
    return {**event, "validation": validation}


def policy_confidence_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    gate = policy_confidence_gate(event["case"], event["agent"]["recommendation"], event["validation"])
    return {**event, "gate": gate}


def route_enriched_case_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    routing = {
        "routing_decision": "ROUTED_TO_ANALYST_QUEUE",
        "queue": event["agent"]["recommendation"]["recommended_queue"],
        "human_approval_required": True,
        "routed_at": _now(),
    }
    return {**event, "routing": routing, "final_status": "ENRICHED_CASE_ROUTED"}


def manual_triage_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    routing = {
        "routing_decision": "MANUAL_TRIAGE",
        "queue": "MANUAL_TRIAGE",
        "reason": event.get("gate", {}).get("reason", "Manual triage required"),
        "routed_at": _now(),
    }
    return {**event, "routing": routing, "final_status": "MANUAL_TRIAGE"}


def escalate_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    routing = {
        "routing_decision": "ESCALATED",
        "queue": "URGENT_MANUAL_REVIEW",
        "reason": event.get("gate", {}).get("reason") or "; ".join(event.get("scope", {}).get("eligibility_reasons", [])),
        "routed_at": _now(),
    }
    return {**event, "routing": routing, "final_status": "ESCALATED"}


def record_audit_state_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    agent = event.get("agent", {})
    recommendation = agent.get("recommendation", {})
    audit = {
        "audit_id": f"AUDIT-{event['case']['exception_id']}-{uuid.uuid4().hex[:8]}",
        "exception_id": event["case"]["exception_id"],
        "execution_arn": event.get("execution_arn") or getattr(context, "invoked_function_arn", "local"),
        "agent_session_id": event.get("agent", {}).get("agent_session_id"),
        "bedrock_model_id": os.environ.get("BEDROCK_MODEL_ID", ""),
        "agent_trace": agent.get("trace", {}),
        "evidence_source_ids": _source_ids(agent.get("evidence", {})),
        "recommendation_evidence_refs": recommendation.get("evidence_refs", []),
        "recommended_queue": recommendation.get("recommended_queue"),
        "playbook_id": recommendation.get("playbook_id"),
        "eligibility_decision": "ELIGIBLE" if event.get("scope", {}).get("eligible") else "INELIGIBLE",
        "validation_decision": event.get("validation", {}).get("decision", "SKIPPED"),
        "routing_decision": event.get("routing", {}).get("routing_decision"),
        "policy_decisions": event.get("agent", {}).get("policy_decisions", []),
        "created_at": _now(),
    }
    _write_audit(audit)
    return {**event, "audit": audit}


def ui_api_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    from urllib.parse import unquote

    path = event.get("rawPath") or event.get("path", "")
    method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod", "GET")
    cors_allowed_origin = os.environ.get("CORS_ALLOWED_ORIGIN", "")
    store = get_data_store()
    status_code = 200
    body: dict[str, Any]
    try:
        if not cors_allowed_origin:
            raise _ApiError(500, "CORS_ALLOWED_ORIGIN is not configured")
        _require_allowed_cognito_audience(event)
        if method == "GET" and path.endswith("/cases"):
            body = {"cases": store.exceptions}
        elif method == "GET" and "/cases/" in path:
            case_key = unquote(path.rsplit("/", 1)[-1])
            body = {"case": store.exception_by_case_key(case_key)}
        elif method == "GET" and path.endswith("/evaluation"):
            body = {"metrics": _read_latest_evaluation_summary()}
        elif method == "GET" and path.endswith("/golden-cases"):
            body = {"cases": _list_golden_cases()}
        elif method == "POST" and path.endswith("/executions"):
            payload = _parse_body(event)
            body, status_code = _start_execution(payload)
        elif method == "GET" and "/executions/" in path:
            identifier = unquote(path.rsplit("/", 1)[-1])
            body, status_code = _describe_execution(identifier)
        else:
            body = {"message": "Agentic post-trade triage API", "request_id": getattr(context, "aws_request_id", "local")}
    except _ApiError as exc:
        body = {"error": exc.message}
        status_code = exc.status_code
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": cors_allowed_origin,
            "access-control-allow-methods": "GET,POST,OPTIONS",
            "access-control-allow-headers": "content-type,authorization",
        },
        "body": json.dumps(body, default=str),
    }


class _ApiError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# Curated, non-technical descriptions for the 8 golden cases. Kept alongside the
# golden dataset so the UI can render friendly titles without having to embed
# business-specific language in JavaScript. The `case_key` values match
# data/golden_dataset.json and data/exceptions.json so the Step Functions path
# does not need to change.
_GOLDEN_CASE_COPY: dict[str, dict[str, str]] = {
    "missing_ssi": {
        "title": "Missing standing settlement instruction",
        "summary": "A same-day US equity settlement can't match at the depository because the counterparty has no settlement instruction on file.",
        "expected_outcome": "The agent routes the case to the SSI remediation queue for analyst review.",
    },
    "unmatched_allocation": {
        "title": "Unmatched trade allocation",
        "summary": "The allocated quantity does not match the traded quantity; the break must be reconciled before settlement.",
        "expected_outcome": "The agent routes the case to allocation operations with full evidence attached.",
    },
    "counterparty_mismatch": {
        "title": "Counterparty profile mismatch",
        "summary": "The counterparty's reference data on the trade disagrees with the current counterparty profile.",
        "expected_outcome": "The agent routes the case to counterparty operations for profile reconciliation.",
    },
    "settlement_status_mismatch": {
        "title": "Conflicting settlement status",
        "summary": "Two upstream systems report different settlement statuses for the same trade. Evidence is inconsistent.",
        "expected_outcome": "The agent escalates — conflicting evidence requires human judgment before routing.",
    },
    "missing_confirmation": {
        "title": "Missing trade confirmation",
        "summary": "An allocated trade lacks a counterparty confirmation, blocking downstream settlement.",
        "expected_outcome": "The agent routes the case to confirmation operations to chase the missing confirmation.",
    },
    "restricted_counterparty": {
        "title": "Restricted counterparty",
        "summary": "The counterparty is on a restricted list. Policy forbids any agent-assisted enrichment.",
        "expected_outcome": "Deterministic escalation — the state machine skips the agent call entirely and escalates on the rule alone.",
    },
    "near_cutoff_escalation": {
        "title": "Near settlement cut-off",
        "summary": "Only 18 minutes remain before the settlement cut-off. Policy escalates any case too close to cut-off.",
        "expected_outcome": "Deterministic escalation — the state machine skips the agent call and escalates to urgent manual review.",
    },
    "stale_reference_data": {
        "title": "Stale reference data",
        "summary": "The security reference data on the trade has not been refreshed in over three months, causing depository mismatch.",
        "expected_outcome": "The agent routes the case to reference-data remediation for a source-data refresh.",
    },
}


# Visible states the UI animates, in canonical order. Choice states ("Eligible?",
# "Route decision") fire instantaneously and are omitted so the animation reads
# cleanly. Terminal routing states (Route/Manual/Escalate) share the same slot
# because exactly one of them runs.
#
# Note: Step Functions rewrites slashes in state names to double dashes inside
# the execution history (it uses slashes as reserved path separators). The
# state machine declares "Policy / confidence met?" but history emits
# "Policy -- confidence met?". We accept both so the UI label stays human-readable.
_UI_STATE_ORDER = [
    "Normalize exception",
    "Scope and severity",
    "Invoke AgentCore",
    "Validate output",
    "Policy / confidence met?",
    "Policy -- confidence met?",
    "Route enriched case",
    "Manual triage",
    "Escalate",
    "Record audit state",
]

# Canonicalization map applied both when matching events and when emitting
# responses. Keeps the UI-facing name stable regardless of what SFN reports.
_STATE_NAME_ALIASES = {
    "Policy -- confidence met?": "Policy / confidence met?",
}


def _list_golden_cases() -> list[dict[str, Any]]:
    store = get_data_store()
    from pathlib import Path
    golden_path = Path(__file__).resolve().parents[2] / "data" / "golden_dataset.json"
    try:
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        golden = []
    enriched: list[dict[str, Any]] = []
    for entry in golden:
        case_key = entry["case_key"]
        copy = _GOLDEN_CASE_COPY.get(case_key, {})
        exception = store.exception_by_case_key(case_key) or {}
        enriched.append(
            {
                "case_key": case_key,
                "exception_id": entry["exception_id"],
                "title": copy.get("title", case_key.replace("_", " ").title()),
                "summary": copy.get("summary", ""),
                "expected_outcome": copy.get("expected_outcome", ""),
                "expected_queue": entry.get("expected_queue"),
                "expected_playbook_id": entry.get("expected_playbook_id"),
                "priority": exception.get("priority"),
                "will_escalate_deterministically": entry.get("expected_escalation") is True
                and case_key in {"restricted_counterparty", "near_cutoff_escalation"},
            }
        )
    return enriched


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise _ApiError(400, f"Invalid JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _ApiError(400, "JSON body must be an object")
    return parsed


def _sfn_client():
    import boto3

    return boto3.client("stepfunctions")


def _start_execution(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if not isinstance(payload, dict):
        raise _ApiError(400, "JSON body must be an object")
    case_key = payload.get("case_key")
    if not isinstance(case_key, str) or not case_key.strip():
        raise _ApiError(400, "case_key is required")
    case_key = case_key.strip()
    if not _CASE_KEY_RE.fullmatch(case_key):
        raise _ApiError(400, "case_key has an unsupported format")
    state_machine_arn = os.environ.get("STATE_MACHINE_ARN", "")
    if not state_machine_arn:
        raise _ApiError(500, "STATE_MACHINE_ARN is not configured on this Lambda")
    # Validate against the golden dataset before hitting Step Functions so we
    # return a friendly 404 instead of a generic SFN failure.
    known = {c["case_key"] for c in _list_golden_cases()}
    if case_key not in known:
        raise _ApiError(404, f"Unknown case_key '{case_key}'")
    execution_name = f"ui-{case_key}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    response = _sfn_client().start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps({"exception": {"case_key": case_key}}),
    )
    return (
        {
            "execution_arn": response["executionArn"],
            "execution_name": execution_name,
            "started_at": response["startDate"].isoformat() if hasattr(response["startDate"], "isoformat") else str(response["startDate"]),
            "case_key": case_key,
        },
        202,
    )


def _describe_execution(identifier: str) -> tuple[dict[str, Any], int]:
    if not isinstance(identifier, str) or not identifier.strip():
        raise _ApiError(400, "execution identifier is required")
    identifier = identifier.strip()
    if len(identifier) > 2048:
        raise _ApiError(400, "execution identifier is too long")
    state_machine_arn = os.environ.get("STATE_MACHINE_ARN", "")
    if not state_machine_arn:
        raise _ApiError(500, "STATE_MACHINE_ARN is not configured on this Lambda")
    # Reconstruct ARN from execution name: arn:aws:states:REGION:ACCOUNT:execution:NAME_OF_SM:EXEC_NAME
    sm_suffix = state_machine_arn.split(":stateMachine:", 1)
    if len(sm_suffix) != 2:
        raise _ApiError(500, "STATE_MACHINE_ARN has an unexpected shape")
    execution_prefix = f"{sm_suffix[0]}:execution:{sm_suffix[1]}:"
    if identifier.startswith("arn:"):
        if not identifier.startswith(execution_prefix):
            raise _ApiError(403, "execution ARN is outside this sample state machine")
        execution_arn = identifier
    else:
        if not _EXECUTION_NAME_RE.fullmatch(identifier):
            raise _ApiError(400, "execution name has an unsupported format")
        execution_arn = f"{sm_suffix[0]}:execution:{sm_suffix[1]}:{identifier}"
    client = _sfn_client()
    try:
        desc = client.describe_execution(executionArn=execution_arn)
    except client.exceptions.ExecutionDoesNotExist:
        raise _ApiError(404, f"Execution not found: {identifier}") from None
    history_events = _fetch_history(client, execution_arn)
    history = _summarize_history(history_events)
    output: Any = None
    if desc.get("status") == "SUCCEEDED" and desc.get("output"):
        try:
            output = json.loads(desc["output"])
        except (TypeError, json.JSONDecodeError):
            output = desc["output"]
    body = {
        "execution_arn": execution_arn,
        "status": desc.get("status"),
        "started_at": desc["startDate"].isoformat() if desc.get("startDate") else None,
        "stopped_at": desc["stopDate"].isoformat() if desc.get("stopDate") else None,
        "history": history,
        "output": output,
        "error": desc.get("error"),
        "cause": desc.get("cause"),
    }
    return body, 200


def _fetch_history(client: Any, execution_arn: str) -> list[dict[str, Any]]:
    # Cap paging to avoid unbounded history reads while keeping the full normal
    # path (the state machine emits ~40 events per execution).
    events: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(5):
        kwargs: dict[str, Any] = {"executionArn": execution_arn, "maxResults": 200, "reverseOrder": False}
        if token:
            kwargs["nextToken"] = token
        page = client.get_execution_history(**kwargs)
        events.extend(page.get("events", []))
        token = page.get("nextToken")
        if not token:
            break
    return events


def _summarize_history(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse raw Step Functions events into one row per visible state.

    Each row has state_name, status (entered|running|exited|failed), entered_at,
    exited_at, and duration_ms. Choice states and the synthetic "ExecutionStarted"
    events are skipped so the UI shows only the steps a non-technical reviewer
    cares about.
    """
    state_rows: dict[str, dict[str, Any]] = {}
    ordered_names: list[str] = []
    for event in events:
        event_type: str = event.get("type", "")
        details = event.get("stateEnteredEventDetails") or event.get("stateExitedEventDetails")
        if not details:
            continue
        name = details.get("name", "")
        if name not in _UI_STATE_ORDER:
            continue
        # Normalize to the canonical human-readable name the UI expects.
        name = _STATE_NAME_ALIASES.get(name, name)
        timestamp = event.get("timestamp")
        ts_iso = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp) if timestamp else None
        row = state_rows.get(name)
        if row is None:
            row = {
                "state_name": name,
                "status": "entered",
                "entered_at": None,
                "exited_at": None,
                "duration_ms": None,
                "output": None,
            }
            state_rows[name] = row
            ordered_names.append(name)
        if event_type == "TaskStateEntered" or event_type == "StateEntered":
            row["entered_at"] = ts_iso
            row["status"] = "running"
        elif event_type == "TaskStateExited" or event_type == "StateExited":
            row["exited_at"] = ts_iso
            row["status"] = "exited"
            if name == "Normalize exception":
                row["output"] = _parse_state_output(details.get("output"))
            if row["entered_at"] and ts_iso:
                try:
                    start = datetime.fromisoformat(row["entered_at"])
                    end = datetime.fromisoformat(ts_iso)
                    row["duration_ms"] = int((end - start).total_seconds() * 1000)
                except ValueError:
                    row["duration_ms"] = None
    # Mark any state that failed (ExecutionFailed / TaskFailed attached to the
    # last running row).
    for event in events:
        if event.get("type", "").endswith("Failed"):
            for row in reversed(list(state_rows.values())):
                if row["status"] == "running":
                    row["status"] = "failed"
                    break
    return [state_rows[name] for name in ordered_names]


def _parse_state_output(raw_output: Any) -> Any:
    if not raw_output:
        return None
    if isinstance(raw_output, str):
        try:
            return json.loads(raw_output)
        except json.JSONDecodeError:
            return raw_output
    return raw_output


def _invoke_agentcore_runtime(agent_runtime_arn: str, case: dict[str, Any]) -> dict[str, Any]:
    import boto3

    client = boto3.client("bedrock-agentcore")
    try:
        return _invoke_once(client, agent_runtime_arn, case)
    except Exception as exc:  # noqa: BLE001 - we classify below
        if not _is_retryable(exc):
            raise
        time.sleep(random.uniform(_MIN_BACKOFF_SECONDS, _MAX_BACKOFF_SECONDS))
        return _invoke_once(client, agent_runtime_arn, case)


def _invoke_once(client: Any, agent_runtime_arn: str, case: dict[str, Any]) -> dict[str, Any]:
    runtime_session_id = f"session-{case['exception_id']}-{uuid.uuid4().hex}"
    response = client.invoke_agent_runtime(
        agentRuntimeArn=agent_runtime_arn,
        qualifier=os.environ.get("AGENT_RUNTIME_QUALIFIER", "DEFAULT"),
        runtimeSessionId=runtime_session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"case": case}).encode("utf-8"),
    )
    body = _read_runtime_body(response.get("response"))
    if not body:
        raise RuntimeInvocationError("empty body from AgentCore Runtime")
    payload = json.loads(body.decode("utf-8"))
    result = payload.get("response", payload)
    if isinstance(result, dict):
        metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
        trace = result.get("trace")
        if not isinstance(trace, dict):
            trace = {}
            result["trace"] = trace
        trace.setdefault(
            "agentcore_runtime",
            {
                "agent_runtime_arn": agent_runtime_arn,
                "qualifier": os.environ.get("AGENT_RUNTIME_QUALIFIER", "DEFAULT"),
                "runtime_session_id": runtime_session_id,
                "request_id": metadata.get("RequestId"),
            },
        )
    return result


def _read_runtime_body(stream: Any) -> bytes:
    """Exhaustively read a botocore StreamingBody (or iterable fallback)."""
    if stream is None:
        return b""
    read = getattr(stream, "read", None)
    if callable(read):
        data = read()
        if isinstance(data, str):
            return data.encode("utf-8")
        return data or b""
    # Fallback: treat as iterable of chunks (bytes or str) for SDK shape changes.
    chunks: list[bytes] = []
    for chunk in stream:
        if isinstance(chunk, bytes):
            chunks.append(chunk)
        else:
            chunks.append(str(chunk).encode("utf-8"))
    return b"".join(chunks)


def _is_retryable(exc: BaseException) -> bool:
    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover - botocore always present at runtime
        return False
    if not isinstance(exc, ClientError):
        return False
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    code = error.get("Code", "")
    if code in _RETRYABLE_ERROR_CODES:
        return True
    metadata = exc.response.get("ResponseMetadata", {}) if isinstance(exc.response, dict) else {}
    if int(metadata.get("HTTPStatusCode", 0)) == 504:
        return True
    if "504" in code:
        return True
    return False


def _write_audit(audit: dict[str, Any]) -> None:
    table_name = os.environ.get("TABLE_NAME", "")
    if not table_name:
        return
    import boto3

    table = boto3.resource("dynamodb").Table(table_name)
    table.put_item(
        Item=decimal_safe(
            {
                "PK": f"EXCEPTION#{audit['exception_id']}",
                "SK": f"AUDIT#{audit['created_at']}",
                "entity_type": "AUDIT",
                "payload": audit,
            }
        ),
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )


def _require_allowed_cognito_audience(event: dict[str, Any]) -> None:
    expected_client_id = os.environ.get("COGNITO_ALLOWED_CLIENT_ID", "")
    if not expected_client_id:
        return
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    token_client_id = claims.get("aud") or claims.get("client_id")
    if token_client_id != expected_client_id:
        raise _ApiError(403, "Token audience is not allowed for this demo API")


def _source_ids(value: Any) -> list[str]:
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            source_id = node.get("source_id")
            if isinstance(source_id, str) and source_id:
                found.add(source_id)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return sorted(found)


def _read_latest_evaluation_summary() -> dict[str, Any]:
    table_name = os.environ.get("TABLE_NAME", "")
    if not table_name:
        return {}
    try:
        import boto3

        table = boto3.resource("dynamodb").Table(table_name)
        response = table.get_item(Key={"PK": "EVALUATION#LATEST", "SK": "SUMMARY"})
        item = response.get("Item", {})
        return item.get("payload", {})
    except Exception:
        return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_normalization_trace(
    raw: dict[str, Any],
    normalized: dict[str, Any],
    inbound_break: dict[str, Any] | None,
) -> dict[str, Any]:
    defaults_applied = []
    for field, default in (
        ("allowed_tool_scope", []),
        ("priority", "MEDIUM"),
        ("status", "OPEN"),
        ("minutes_to_cutoff", 240),
    ):
        if field not in raw:
            defaults_applied.append({"field": field, "value": default, "reason": "Defaulted by Normalize exception"})
    if "case_key" not in raw:
        defaults_applied.append(
            {
                "field": "case_key",
                "value": normalized["case_key"],
                "reason": "Derived from exception_id when source payload omits case_key",
            }
        )

    required_fields = [
        "exception_id",
        "exception_type",
        "product",
        "market",
        "desk",
        "counterparty_id",
        "account_id",
        "settlement_date",
    ]
    checks = [
        {
            "check": "Normalized schema contract",
            "result": "PASS",
            "detail": f"{len(required_fields)} required fields present",
        },
        {
            "check": "Golden case resolved",
            "result": "PASS",
            "detail": f"{normalized['case_key']} -> {normalized['exception_id']}",
        },
    ]
    if inbound_break:
        checks.extend(inbound_break.get("validation_checks", []))

    return {
        "source_break": inbound_break,
        "defaults_applied": defaults_applied,
        "normalized_contract": {
            "case_key": normalized["case_key"],
            "exception_id": normalized["exception_id"],
            "exception_type": normalized["exception_type"],
            "priority": normalized.get("priority"),
            "status": normalized.get("status"),
            "product": normalized["product"],
            "market": normalized["market"],
            "desk": normalized["desk"],
            "counterparty_id": normalized["counterparty_id"],
            "account_id": normalized["account_id"],
            "security_id": normalized.get("security_id"),
            "settlement_date": normalized["settlement_date"],
            "minutes_to_cutoff": normalized.get("minutes_to_cutoff"),
            "allowed_tool_scope": normalized.get("allowed_tool_scope", []),
        },
        "validation_checks": checks,
    }
