from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.agentcore_runtime.triage_workflow import run_triage, run_triage_streamed


app = FastAPI(title="Agentic Post-Trade Exception Triage Runtime")


class InvocationRequest(BaseModel):
    case: dict[str, Any] = Field(default_factory=dict)


@app.get("/ping")
def ping() -> dict[str, Any]:
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


def _wants_sse(request: Request) -> bool:
    """Content-negotiate on Accept. text/event-stream → streamed stage events.

    Anything else (including the default application/json) keeps the existing
    aggregated behavior that Step Functions and the evaluator depend on. This
    is the hinge of the feature flag: whether streaming is used is decided at
    invocation time by the caller, not at deploy time by the runtime.
    """
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept.lower()


def _sse_stream(case: dict[str, Any]):
    """Encode run_triage_streamed() as Server-Sent Events.

    Each stage event becomes one SSE message of the form:
        event: <event_name>
        data: <JSON-serialized payload>
        \n
    """
    for event in run_triage_streamed(case):
        event_name = event.get("event", "message")
        # Exclude the event-name key from the data payload; the field is
        # redundant once it's part of the SSE event line.
        data = {k: v for k, v in event.items() if k != "event"}
        payload = json.dumps(data, default=str)
        yield f"event: {event_name}\ndata: {payload}\n\n"


@app.post("/invocations")
async def invoke(request: Request):
    body = await request.json()
    case = (body or {}).get("case") or {}
    if _wants_sse(request):
        # Headers required by AWS Lambda Web Adapter + API Gateway streaming:
        # - Content-Type: text/event-stream signals SSE to the browser.
        # - Cache-Control: no-cache prevents intermediate caching of partial
        #   streams.
        # - X-Accel-Buffering: no disables nginx-style proxy buffering if any
        #   layer in front of us adds it.
        return StreamingResponse(
            _sse_stream(case),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return {"status": "success", "response": run_triage(case)}


if __name__ == "__main__":
    import uvicorn

    # AgentCore Runtime HTTP protocol listens on port 8080. The managed hosting
    # layer must be able to reach the process inside the runtime environment.
    # The service endpoint remains AgentCore-managed; this does not expose a
    # public unauthenticated listener from the sample account.
    uvicorn.run("src.agentcore_runtime.app:app", host="0.0.0.0", port=8080)  # nosec B104
