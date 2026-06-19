from __future__ import annotations

import json
from typing import Any

import pytest

from src.common.data_store import get_data_store
from src.common.triage_rules import normalize_exception
from src.lambda_tasks import handlers


class FakeStreamingBody:
    """Minimal stand-in for botocore.response.StreamingBody."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._consumed = False

    def read(self) -> bytes:
        if self._consumed:
            return b""
        self._consumed = True
        return self._data


def _fake_recommendation(exception_id: str) -> dict[str, Any]:
    return {
        "exception_id": exception_id,
        "root_cause_category": "MISSING_OR_STALE_SSI",
        "recommended_queue": "SSI_REMEDIATION",
        "recommended_action": "Refresh SSI; route for human analyst review.",
        "confidence": 0.9,
        "evidence_refs": ["ssi_record:CP-SYN-4421"],
        "playbook_id": "PB-SSI-001",
        "human_approval_required": True,
        "policy_notes": ["Read-only tools used"],
        "escalation_reason": None,
    }


def _runtime_payload(exception_id: str) -> bytes:
    return json.dumps(
        {
            "status": "success",
            "response": {
                "exception_id": exception_id,
                "agent_session_id": f"session-{exception_id}",
                "recommendation": _fake_recommendation(exception_id),
                "evidence": {},
                "policy_decisions": [],
                "trace": {"stages": ["summary"]},
            },
        }
    ).encode("utf-8")


def _case() -> dict[str, Any]:
    return normalize_exception(get_data_store().exception_by_case_key("missing_ssi"))


def test_invoke_agentcore_handler_reads_streaming_body(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test",
    )
    case = _case()
    captured_kwargs: dict[str, Any] = {}

    class FakeClient:
        def invoke_agent_runtime(self, **kwargs):
            captured_kwargs.update(kwargs)
            return {
                "response": FakeStreamingBody(_runtime_payload(case["exception_id"])),
                "contentType": "application/json",
                "ResponseMetadata": {"HTTPStatusCode": 200},
            }

    def fake_boto3_client(service_name: str, *args, **kwargs):
        assert service_name == "bedrock-agentcore"
        return FakeClient()

    import boto3

    monkeypatch.setattr(boto3, "client", fake_boto3_client)

    result = handlers.invoke_agentcore_handler({"case": case}, None)

    assert captured_kwargs["agentRuntimeArn"].endswith("runtime/test")
    assert captured_kwargs["contentType"] == "application/json"
    recommendation = result["agent"]["recommendation"]
    assert recommendation["exception_id"] == case["exception_id"]
    assert recommendation["playbook_id"] == "PB-SSI-001"
    assert recommendation["recommended_queue"] == "SSI_REMEDIATION"


def test_invoke_agentcore_handler_retries_once_on_throttling(monkeypatch):
    monkeypatch.setenv(
        "AGENT_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/test",
    )
    # No real sleep while testing retry behavior.
    monkeypatch.setattr(handlers.time, "sleep", lambda _seconds: None)

    case = _case()
    call_count = {"n": 0}

    from botocore.exceptions import ClientError

    class FlakyClient:
        def invoke_agent_runtime(self, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ClientError(
                    {
                        "Error": {"Code": "ThrottlingException", "Message": "slow down"},
                        "ResponseMetadata": {"HTTPStatusCode": 429},
                    },
                    "InvokeAgentRuntime",
                )
            return {
                "response": FakeStreamingBody(_runtime_payload(case["exception_id"])),
                "contentType": "application/json",
                "ResponseMetadata": {"HTTPStatusCode": 200},
            }

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *_a, **_kw: FlakyClient())

    result = handlers.invoke_agentcore_handler({"case": case}, None)

    assert call_count["n"] == 2
    assert result["agent"]["recommendation"]["playbook_id"] == "PB-SSI-001"


def test_invoke_agentcore_runtime_raises_on_empty_body(monkeypatch):
    case = _case()

    class EmptyClient:
        def invoke_agent_runtime(self, **_kwargs):
            return {
                "response": FakeStreamingBody(b""),
                "contentType": "application/json",
                "ResponseMetadata": {"HTTPStatusCode": 200},
            }

    import boto3

    monkeypatch.setattr(boto3, "client", lambda *_a, **_kw: EmptyClient())

    with pytest.raises(handlers.RuntimeInvocationError):
        handlers._invoke_agentcore_runtime("arn:aws:bedrock-agentcore:us-east-1:0:runtime/x", case)
