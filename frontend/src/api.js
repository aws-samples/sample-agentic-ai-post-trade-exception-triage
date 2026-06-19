// Thin API client for the Triage UI. All calls go to the API Gateway URL
// surfaced at runtime via window.__TRIAGE_CONFIG.apiUrl, so the same built
// bundle works against any deployed stack.

import { authHeaders } from "./auth";

const config = typeof window !== "undefined" ? window.__TRIAGE_CONFIG || {} : {};
const API_URL = (config.apiUrl || "").replace(/\/$/, "");

function joinUrl(path) {
  if (!API_URL) {
    throw new Error("Triage API URL is not configured (missing /config.js).");
  }
  return `${API_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

async function handle(response) {
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { raw: text };
    }
  }
  if (!response.ok) {
    const message = body?.error || body?.message || `Request failed with ${response.status}`;
    const err = new Error(message);
    err.status = response.status;
    err.body = body;
    throw err;
  }
  return body;
}

export async function fetchGoldenCases() {
  const response = await fetch(joinUrl("/golden-cases"), { headers: { accept: "application/json", ...authHeaders() } });
  const body = await handle(response);
  return body?.cases || [];
}

export async function fetchEvaluationMetrics() {
  const response = await fetch(joinUrl("/evaluation"), { headers: { accept: "application/json", ...authHeaders() } });
  const body = await handle(response);
  return body?.metrics || null;
}

export async function startExecution(caseKey) {
  const response = await fetch(joinUrl("/executions"), {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json", ...authHeaders() },
    body: JSON.stringify({ case_key: caseKey }),
  });
  return handle(response);
}

export async function describeExecution(executionArn) {
  const identifier = encodeURIComponent(executionArn);
  const response = await fetch(joinUrl(`/executions/${identifier}`), { headers: { accept: "application/json", ...authHeaders() } });
  return handle(response);
}

// Response-streaming feature endpoint. Returns a string URL the UI can fetch as
// text/event-stream. Returns null when the feature is disabled at deploy time,
// which lets callers short-circuit without guessing.
export function streamingEndpointFor(caseKey) {
  const cfg = typeof window !== "undefined" ? window.__TRIAGE_CONFIG || {} : {};
  if (!cfg.responseStreaming) return null;
  const base = cfg.streamingEndpointUrl;
  if (!base) return null;
  const sep = base.includes("?") ? "&" : "?";
  return `${base}${sep}case_key=${encodeURIComponent(caseKey)}`;
}

export function connectStageStream(caseKey, handlers) {
  const url = streamingEndpointFor(caseKey);
  if (!url) return null;
  const controller = new AbortController();
  let buffer = "";

  async function start() {
    try {
      const response = await fetch(url, {
        headers: { accept: "text/event-stream", ...authHeaders() },
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`Streaming endpoint failed with ${response.status}`);
      }
      handlers.onOpen?.();
      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error("Streaming endpoint returned no readable body");
      }
      const decoder = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        buffer = drainSseBuffer(buffer, handlers);
      }
      buffer += decoder.decode();
      drainSseBuffer(`${buffer}\n\n`, handlers);
      handlers.onComplete?.();
    } catch (err) {
      if (controller.signal.aborted) return;
      handlers.onError?.(err);
    }
  }

  start();
  return { close: () => controller.abort() };
}

function drainSseBuffer(input, handlers) {
  const frames = input.split(/\n\n/);
  const tail = frames.pop() || "";
  frames.forEach((frame) => dispatchSseFrame(frame, handlers));
  return tail;
}

function dispatchSseFrame(frame, handlers) {
  const lines = frame.split(/\r?\n/);
  let event = "message";
  const data = [];
  lines.forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  });
  const rawData = data.join("\n");
  handlers.onEvent?.({ event, data: rawData });
}
