"""Tests for the Runtime's dual-mode invocation path.

These guard two properties the response-streaming feature depends on:

  1. run_triage and run_triage_streamed produce the same final response for
     the same input. If they ever drift, the UI (streaming path) would show
     a different recommendation than the authoritative Step Functions path
     — that would silently regress the "deterministic gates / validation
     authoritative" correctness property.

  2. run_triage_streamed yields stages in the canonical order and ends with
     a terminal `complete` event that carries the full aggregated response.

All tests run under DISABLE_STRANDS_MODEL_CALL=1 so deterministic stage
output is used. That is identical to how the other tests in this suite
exercise the runtime.
"""

from __future__ import annotations

import copy
import os
from typing import Any

os.environ.setdefault("DISABLE_STRANDS_MODEL_CALL", "1")
os.environ.setdefault("AGENTCORE_GATEWAY_LOCAL_MODE", "1")

from src.agentcore_runtime.triage_workflow import (  # noqa: E402  (env var first)
    STAGE_ORDER,
    run_triage,
    run_triage_streamed,
)
from src.common.data_store import get_data_store  # noqa: E402
from src.common.triage_rules import normalize_exception  # noqa: E402


def _case(case_key: str) -> dict[str, Any]:
    raw = get_data_store().exception_by_case_key(case_key)
    assert raw is not None, f"unknown synthetic case_key {case_key!r}"
    return normalize_exception(raw)


def _collect(generator) -> list[dict[str, Any]]:
    return [copy.deepcopy(event) for event in generator]


def _strip_timings(response: dict[str, Any]) -> dict[str, Any]:
    """Remove the fields that are inherently timing-dependent so two runs on
    the same input compare equal."""
    scrubbed = copy.deepcopy(response)
    trace = scrubbed.get("trace", {})
    for k in ("started_at", "completed_at", "latency_ms"):
        trace.pop(k, None)
    return scrubbed


def test_streamed_final_matches_aggregated_for_missing_ssi() -> None:
    """The streamed final response must match the aggregated response exactly
    (modulo timing fields), so the UI never shows a different recommendation
    than Step Functions records."""
    case = _case("missing_ssi")
    aggregated = run_triage(case)

    events = _collect(run_triage_streamed(case))
    assert events, "streamed generator produced no events"
    assert events[-1]["event"] == "complete"
    streamed_final = events[-1]["response"]

    assert _strip_timings(streamed_final) == _strip_timings(aggregated)


def test_streamed_stage_order_matches_canonical_order() -> None:
    """Every stage runs exactly once, in the canonical order, with a
    running→done pair per stage."""
    case = _case("missing_ssi")
    events = _collect(run_triage_streamed(case))

    stage_events = [e for e in events if e["event"] == "stage"]
    # Each stage emits one running and one done event.
    assert [e["stage"] for e in stage_events if e["status"] == "running"] == list(STAGE_ORDER)
    assert [e["stage"] for e in stage_events if e["status"] == "done"] == list(STAGE_ORDER)


def test_streamed_done_events_carry_stage_output() -> None:
    """Each `done` event exposes the stage's output so the SSE UI can render
    something richer than status-only — this is the "richer" payload choice
    the design locked in."""
    case = _case("missing_ssi")
    events = _collect(run_triage_streamed(case))

    done_by_stage = {e["stage"]: e for e in events if e.get("event") == "stage" and e.get("status") == "done"}
    for stage in STAGE_ORDER:
        assert stage in done_by_stage, f"missing done event for {stage}"
        assert "output" in done_by_stage[stage], f"done event for {stage} missing output"


def test_streamed_complete_event_has_full_trace() -> None:
    """The terminal `complete` event must carry the full trace (stages,
    framework, latency) so the UI can display post-run metadata without a
    second API call."""
    case = _case("missing_ssi")
    events = _collect(run_triage_streamed(case))

    final = events[-1]
    assert final["event"] == "complete"
    trace = final["response"]["trace"]
    assert trace["framework"] == "strands-agents"
    assert trace["model_provider"] == "amazon-bedrock"
    assert trace["stages"] == list(STAGE_ORDER)
    assert isinstance(trace["latency_ms"], int)
    assert trace["latency_ms"] >= 0
