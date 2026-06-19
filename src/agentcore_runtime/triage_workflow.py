from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

from src.agentcore_runtime.agents.evidence_agent import run_evidence_stage
from src.agentcore_runtime.agents.playbook_agent import run_playbook_stage
from src.agentcore_runtime.agents.recommendation_agent import run_recommendation_stage
from src.agentcore_runtime.agents.summary_agent import run_summary_stage


# Canonical stage order for both the aggregated and streamed code paths. Keep
# this in one place so the tests in tests/test_streaming.py can assert that
# `run_triage_streamed` yields stages in the same order `run_triage` records
# in `trace.stages`.
STAGE_ORDER: tuple[str, ...] = ("summary", "evidence", "playbook", "recommendation")


def _build_trace(started: datetime, finished: datetime) -> dict[str, Any]:
    return {
        "framework": "strands-agents",
        "model_provider": "amazon-bedrock",
        "started_at": started.isoformat(),
        "completed_at": finished.isoformat(),
        "latency_ms": int((finished - started).total_seconds() * 1000),
        "stages": list(STAGE_ORDER),
    }


def run_triage(case: dict[str, Any]) -> dict[str, Any]:
    """Run all four stages and return the aggregated final response.

    This is the authoritative path consumed by Step Functions' InvokeAgentCore
    Lambda and by the evaluation runner. Behavior is preserved byte-for-byte
    from the pre-streaming version so Task 13 thresholds stay directly
    comparable across the feature flag.
    """
    started = datetime.now(timezone.utc)
    summary = run_summary_stage(case)
    evidence_package = run_evidence_stage(case, summary)
    playbook_match = run_playbook_stage(case, summary, evidence_package)
    recommendation = run_recommendation_stage(case, evidence_package, playbook_match)
    finished = datetime.now(timezone.utc)
    return {
        "exception_id": case["exception_id"],
        "agent_session_id": f"session-{case['exception_id']}",
        "summary": summary,
        "evidence": evidence_package["evidence"],
        "policy_decisions": evidence_package["policy_decisions"],
        "playbook_match": playbook_match,
        "recommendation": recommendation,
        "trace": _build_trace(started, finished),
    }


def run_triage_streamed(case: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Run the four stages, yielding one event per stage plus a final event.

    Emitted events are dicts with the shape:
      { "event": "stage", "stage": "<name>", "status": "running", "started_at": "..." }
      { "event": "stage", "stage": "<name>", "status": "done",    "started_at": "...", "completed_at": "...", "latency_ms": N, "output": {...} }
      { "event": "complete", "response": <same dict returned by run_triage()> }

    The terminal `complete` event carries the full aggregated response so that
    the SSE client can validate and persist the final recommendation without
    reconstructing state from deltas. Correctness properties are preserved:
    the streamed path returns identical final output to run_triage(), which
    the unit tests assert.
    """
    pipeline_started = datetime.now(timezone.utc)

    summary_started = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "summary",
        "status": "running",
        "started_at": summary_started.isoformat(),
    }
    summary = run_summary_stage(case)
    summary_finished = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "summary",
        "status": "done",
        "started_at": summary_started.isoformat(),
        "completed_at": summary_finished.isoformat(),
        "latency_ms": int((summary_finished - summary_started).total_seconds() * 1000),
        "output": summary,
    }

    evidence_started = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "evidence",
        "status": "running",
        "started_at": evidence_started.isoformat(),
    }
    evidence_package = run_evidence_stage(case, summary)
    evidence_finished = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "evidence",
        "status": "done",
        "started_at": evidence_started.isoformat(),
        "completed_at": evidence_finished.isoformat(),
        "latency_ms": int((evidence_finished - evidence_started).total_seconds() * 1000),
        "output": {
            "evidence": evidence_package["evidence"],
            "policy_decisions": evidence_package["policy_decisions"],
        },
    }

    playbook_started = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "playbook",
        "status": "running",
        "started_at": playbook_started.isoformat(),
    }
    playbook_match = run_playbook_stage(case, summary, evidence_package)
    playbook_finished = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "playbook",
        "status": "done",
        "started_at": playbook_started.isoformat(),
        "completed_at": playbook_finished.isoformat(),
        "latency_ms": int((playbook_finished - playbook_started).total_seconds() * 1000),
        "output": playbook_match,
    }

    recommendation_started = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "recommendation",
        "status": "running",
        "started_at": recommendation_started.isoformat(),
    }
    recommendation = run_recommendation_stage(case, evidence_package, playbook_match)
    recommendation_finished = datetime.now(timezone.utc)
    yield {
        "event": "stage",
        "stage": "recommendation",
        "status": "done",
        "started_at": recommendation_started.isoformat(),
        "completed_at": recommendation_finished.isoformat(),
        "latency_ms": int((recommendation_finished - recommendation_started).total_seconds() * 1000),
        "output": recommendation,
    }

    pipeline_finished = datetime.now(timezone.utc)
    final_response: dict[str, Any] = {
        "exception_id": case["exception_id"],
        "agent_session_id": f"session-{case['exception_id']}",
        "summary": summary,
        "evidence": evidence_package["evidence"],
        "policy_decisions": evidence_package["policy_decisions"],
        "playbook_match": playbook_match,
        "recommendation": recommendation,
        "trace": _build_trace(pipeline_started, pipeline_finished),
    }
    yield {"event": "complete", "response": final_response}
