"""Streaming Lambda (feature: responseStreaming).

Fronted by AWS Lambda Web Adapter in streaming mode. Receives a GET request
from API Gateway with a case_key query param, invokes the deployed AgentCore
Runtime requesting text/event-stream, and re-emits each SSE frame to the
caller as it arrives. This is a UI-feedback path only; the authoritative
Step Functions execution runs in parallel through the non-streaming path and
remains the record of truth.

Considered trade-off (documented in docs/DEPLOYMENT.md and requirements
Requirement 15): while this feature is enabled, a UI "Run triage" click
incurs two AgentCore Runtime invocations per case — one through this
function for live UI feedback, one through Step Functions for the audit
record. Disable the responseStreaming CDK context flag to avoid the double
spend at the cost of losing live stage-by-stage UI feedback.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Any

import boto3
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.common.data_store import get_data_store
from src.common.triage_rules import normalize_exception


LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Post-Trade Triage Streaming Invoke")

ALLOWED_ORIGIN = os.environ.get("CORS_ALLOWED_ORIGIN", "")
AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
AGENT_RUNTIME_QUALIFIER = os.environ.get("AGENT_RUNTIME_QUALIFIER", "DEFAULT")


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "GET,OPTIONS",
        "Access-Control-Allow-Headers": "content-type,authorization",
    }


@app.get("/ping")
def ping() -> dict[str, Any]:
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


def _resolve_case(case_key: str) -> dict[str, Any]:
    if case_key not in _golden_case_keys():
        raise HTTPException(status_code=404, detail=f"Unknown golden case_key '{case_key}'")
    store = get_data_store()
    raw = store.exception_by_case_key(case_key)
    if not raw:
        raise HTTPException(status_code=404, detail=f"Unknown case_key '{case_key}'")
    return normalize_exception(raw)


def _golden_case_keys() -> set[str]:
    return {
        item["case_key"]
        for item in get_data_store().golden_dataset
        if isinstance(item.get("case_key"), str) and item["case_key"]
    }


def _iter_runtime_chunks(case: dict[str, Any]):
    """Invoke AgentCore Runtime with SSE accept and yield chunks verbatim.

    The Runtime's /invocations handler, when it sees `Accept: text/event-stream`,
    returns a FastAPI StreamingResponse that emits one SSE frame per stage
    plus a terminal `event: complete` frame. AgentCore Runtime passes those
    bytes through to invoke_agent_runtime's response StreamingBody. We just
    forward the bytes.
    """
    if not AGENT_RUNTIME_ARN:
        raise HTTPException(status_code=500, detail="AGENT_RUNTIME_ARN is not configured")
    client = boto3.client("bedrock-agentcore")
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier=AGENT_RUNTIME_QUALIFIER,
        runtimeSessionId=f"stream-{case['exception_id']}-{uuid.uuid4().hex}",
        contentType="application/json",
        accept="text/event-stream",
        payload=json.dumps({"case": case}).encode("utf-8"),
    )
    stream = response.get("response")
    if stream is None:
        raise HTTPException(status_code=502, detail="AgentCore Runtime returned no body")
    # Yield in small chunks so LWA flushes them immediately. Fall back to
    # iter_chunks if the botocore StreamingBody is iterable.
    iter_chunks = getattr(stream, "iter_chunks", None)
    if callable(iter_chunks):
        for chunk in iter_chunks(chunk_size=1024):
            if chunk:
                yield chunk
    else:
        data = stream.read() if hasattr(stream, "read") else stream
        if isinstance(data, str):
            data = data.encode("utf-8")
        yield data


@app.get("/stream")
@app.get("/executions-stream")
def stream(case_key: str = Query(..., min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")) -> StreamingResponse:
    try:
        if not ALLOWED_ORIGIN:
            raise HTTPException(status_code=500, detail="CORS_ALLOWED_ORIGIN is not configured")
        case = _resolve_case(case_key)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Failed to resolve case %s", case_key)
        raise HTTPException(status_code=500, detail=f"Failed to resolve case: {exc}") from exc

    def generator():
        # Initial event: tell the client which case we started, so the UI
        # can correlate the stream to the card it clicked.
        yield f"event: started\ndata: {json.dumps({'case_key': case_key, 'exception_id': case['exception_id']})}\n\n".encode("utf-8")
        try:
            for chunk in _iter_runtime_chunks(case):
                yield chunk
        except Exception as exc:  # noqa: BLE001 - surface any runtime error as an SSE error event
            LOGGER.exception("Streaming runtime invocation failed for %s", case_key)
            yield f"event: error\ndata: {json.dumps({'message': str(exc)})}\n\n".encode("utf-8")

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        **_cors_headers(),
    }
    return StreamingResponse(generator(), media_type="text/event-stream", headers=headers)
