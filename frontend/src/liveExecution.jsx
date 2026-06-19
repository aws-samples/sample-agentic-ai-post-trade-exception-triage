import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Box,
  Container,
  ExpandableSection,
  Header,
  ProgressBar,
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import { ArrowRight, CheckCircle2, Circle, FileText, Loader2, Sparkles, XCircle } from "lucide-react";
import { connectStageStream, describeExecution } from "./api";

// Canonical state order displayed to the user. Matches the state names on the
// state machine definition in infra/triage_stack.py. Terminal routing states
// (Route enriched case | Manual triage | Escalate) share a single slot at
// position 6 because exactly one of them ever runs for a given execution.
const TIMELINE_STEPS = [
  { key: "Normalize exception", label: "1. Normalize exception", hint: "Read the inbound break file row, validate required fields, and produce the normalized case contract." },
  { key: "Scope and severity", label: "2. Scope and severity", hint: "Deterministic gates decide if the case is eligible for agent assistance." },
  { key: "Invoke AgentCore", label: "3. Invoke AgentCore", hint: "Strands Agents + Claude Opus 4.6 run summary, evidence, playbook, recommendation." },
  { key: "Validate output", label: "4. Validate output", hint: "Schema + cross-reference checks against the deterministic case data." },
  { key: "Policy / confidence met?", label: "5. Policy and confidence gate", hint: "Apply policy rules and a confidence threshold; decides route vs. escalate vs. manual." },
  { key: "__routing__", label: "6. Route the case", hint: "Exactly one of Route enriched case, Manual triage, or Escalate." },
  { key: "Record audit state", label: "7. Record audit state", hint: "Persist the full trace to DynamoDB for auditability." },
];

// Map the single "routing" slot to whichever concrete state actually ran.
const ROUTING_STATES = new Set(["Route enriched case", "Manual triage", "Escalate"]);

function pickTimelineRows(history) {
  const byName = new Map();
  (history || []).forEach((row) => byName.set(row.state_name, row));
  return TIMELINE_STEPS.map((step) => {
    if (step.key === "__routing__") {
      // Find whichever routing state ran (if any).
      for (const name of ROUTING_STATES) {
        const row = byName.get(name);
        if (row) return { ...step, concreteName: name, row };
      }
      return { ...step, concreteName: null, row: null };
    }
    return { ...step, concreteName: step.key, row: byName.get(step.key) || null };
  });
}

function formatDuration(ms) {
  if (ms == null) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function formatValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  if (value == null || value === "") return "-";
  return String(value);
}

function NormalizeBreakPanel({ output }) {
  const trace = output?.normalization;
  const source = trace?.source_break;
  if (!trace || !source) return null;

  const rawRows = Object.entries(source.raw_record || {}).map(([field, value]) => ({ field, value: formatValue(value) }));
  const mappingRows = Array.isArray(source.field_mapping) ? source.field_mapping : [];
  const checks = Array.isArray(trace.validation_checks) ? trace.validation_checks : [];
  const defaults = Array.isArray(trace.defaults_applied) ? trace.defaults_applied : [];
  const contract = trace.normalized_contract || {};
  const fileProfile = source.file_profile || {};
  const contractRows = [
    ["exception_id", contract.exception_id],
    ["exception_type", contract.exception_type],
    ["priority", contract.priority],
    ["status", contract.status],
    ["counterparty_id", contract.counterparty_id],
    ["account_id", contract.account_id],
    ["settlement_date", contract.settlement_date],
    ["minutes_to_cutoff", contract.minutes_to_cutoff],
    ["allowed_tool_scope", contract.allowed_tool_scope],
  ].map(([field, value]) => ({ field, value: formatValue(value) }));

  return (
    <div className="normalize-panel">
      <div className="normalize-hero">
        <div className="normalize-file-icon">
          <FileText size={22} />
        </div>
        <div className="normalize-file-main">
          <div className="normalize-kicker">Inbound break file</div>
          <div className="normalize-file-name">{source.file_name}</div>
          <div className="normalize-file-meta">
            <span>{source.source_system}</span>
            <span>row {source.record_number}</span>
            <span>{source.received_at}</span>
          </div>
        </div>
        <Badge color="blue">{fileProfile.format || "file"} / {fileProfile.delimiter || ","}</Badge>
      </div>

      <div className="normalize-stats">
        <div>
          <Box variant="awsui-key-label">Batch</Box>
          <Box fontWeight="bold">{source.batch_id}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Records in file</Box>
          <Box fontWeight="bold">{fileProfile.record_count || "-"}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Checksum</Box>
          <Box fontSize="body-s">{fileProfile.checksum || "-"}</Box>
        </div>
      </div>

      <div className="normalize-flow">
        <div className="normalize-block">
          <div className="normalize-block-title">Raw row from internal system</div>
          <div className="raw-field-grid">
            {rawRows.map((row) => (
              <div key={row.field} className={`raw-field ${isWideNormalizeField(row.field) ? "raw-field-wide" : ""}`}>
                <span>{row.field}</span>
                <strong>{row.value}</strong>
              </div>
            ))}
          </div>
        </div>
        <div className="normalize-arrow" aria-hidden="true">
          <ArrowRight size={22} />
        </div>
        <div className="normalize-block normalize-contract">
          <div className="normalize-block-title">Normalized case contract</div>
          <div className="raw-field-grid">
            {contractRows.map((row) => (
              <div key={row.field} className={`raw-field ${isWideNormalizeField(row.field) ? "raw-field-wide" : ""}`}>
                <span>{row.field}</span>
                <strong>{row.value}</strong>
              </div>
            ))}
          </div>
        </div>
      </div>

      <ExpandableSection headerText="Field mapping and validation" variant="footer">
        <div className="mapping-list">
          {mappingRows.map((row) => (
            <div key={`${row.source_field}-${row.target_field}`} className="mapping-row">
              <code>{row.source_field}</code>
              <ArrowRight size={14} />
              <code>{row.target_field}</code>
              <span>{formatValue(row.raw_value)}</span>
              <span>{formatValue(row.normalized_value)}</span>
              <small>{row.transform}</small>
            </div>
          ))}
        </div>
        {defaults.length ? (
          <div className="normalize-checks">
            {defaults.map((item) => (
              <Badge key={item.field} color="grey">
                default {item.field} = {formatValue(item.value)}
              </Badge>
            ))}
          </div>
        ) : null}
        <div className="normalize-checks">
          {checks.map((item) => (
            <Badge key={`${item.check}-${item.detail}`} color={item.result === "PASS" ? "green" : "red"}>
              {item.check}
            </Badge>
          ))}
        </div>
      </ExpandableSection>
    </div>
  );
}

function isWideNormalizeField(field) {
  return ["brk_desc", "allow_tools", "exception_type", "allowed_tool_scope"].includes(field);
}

// --- Streaming sub-panel (feature: responseStreaming) -----------------------
//
// Shown beneath the "Invoke AgentCore" step while that step is running. Opens
// a fetch stream against /executions-stream?case_key=... and renders the four
// agent stages (summary, evidence, playbook, recommendation) with per-stage
// status, elapsed time, and a richer payload once the stage completes.
//
// The fetch stream is parallel to (not dependent on) the Step Functions
// execution the main panel is polling. Both hit the same AgentCore Runtime,
// which is the documented double-invocation trade-off of responseStreaming=true.

const STREAM_STAGES = [
  { key: "summary", label: "Summarize", hint: "Read the case and list the evidence the next stage should fetch." },
  { key: "evidence", label: "Retrieve evidence", hint: "Call read-only tools to hydrate trade, settlement, allocation, SSI, prior cases, playbook." },
  { key: "playbook", label: "Match playbook", hint: "Map the likely root cause onto an approved playbook." },
  { key: "recommendation", label: "Draft recommendation", hint: "Produce the advisory recommendation. Human approval stays required." },
];

function summariseStageOutput(stage, output) {
  // Short, human-readable teaser per stage. Kept tight for the live demo —
  // the full output lands in the recommendation panel once the SFN execution
  // finishes.
  if (!output || typeof output !== "object") return null;
  if (stage === "summary") {
    const cat = output.likely_root_cause_category;
    const needs = Array.isArray(output.evidence_needs) ? output.evidence_needs.length : 0;
    return cat ? `Likely root cause: ${cat}${needs ? ` · ${needs} evidence items identified` : ""}` : null;
  }
  if (stage === "evidence") {
    const evidence = output.evidence || {};
    const tools = Object.keys(evidence).filter((k) => evidence[k]);
    const policyDecisions = Array.isArray(output.policy_decisions) ? output.policy_decisions.length : 0;
    return tools.length ? `Retrieved ${tools.length} evidence items · ${policyDecisions} policy decisions` : null;
  }
  if (stage === "playbook") {
    const pb = output.playbook || {};
    return pb.playbook_id ? `Matched playbook ${pb.playbook_id} → ${pb.queue || "queue"}` : null;
  }
  if (stage === "recommendation") {
    const conf = output.confidence;
    const queue = output.recommended_queue;
    const playbook = output.playbook_id;
    if (queue && playbook) {
      const confStr = typeof conf === "number" ? ` · confidence ${Math.round(conf * 100)}%` : "";
      return `${playbook} → ${queue}${confStr}`;
    }
    return null;
  }
  return null;
}

function StageCard({ stage, entry, elapsedMs }) {
  const status = entry?.status || "waiting";
  let icon;
  let color;
  let badgeText = "";
  if (status === "done") {
    icon = <CheckCircle2 size={18} color="#0f7d3b" />;
    color = "green";
    badgeText = formatDuration(entry?.latency_ms);
  } else if (status === "running") {
    icon = <Loader2 size={18} color="#0972d3" className="spin" />;
    color = "blue";
    badgeText = elapsedMs != null ? `${formatDuration(elapsedMs)} in flight` : "in flight";
  } else if (status === "error") {
    icon = <XCircle size={18} color="#c02828" />;
    color = "red";
    badgeText = "Failed";
  } else {
    icon = <Circle size={18} color="#8c8c8c" />;
    color = "grey";
  }
  const teaser = status === "done" ? summariseStageOutput(stage.key, entry?.output) : null;
  return (
    <div className={`stream-stage stream-stage-${status}`}>
      <div className="stream-stage-row">
        <span className="stream-stage-icon">{icon}</span>
        <span className="stream-stage-label">{stage.label}</span>
        {badgeText ? <Badge color={color}>{badgeText}</Badge> : null}
      </div>
      <div className="stream-stage-hint">{stage.hint}</div>
      {teaser ? <div className="stream-stage-teaser">{teaser}</div> : null}
      {status === "done" && entry?.output ? (
        <ExpandableSection headerText="Stage output (full)" variant="footer" defaultExpanded>
          <pre className="stream-stage-pre">{JSON.stringify(entry.output, null, 2)}</pre>
        </ExpandableSection>
      ) : null}
    </div>
  );
}

function StageStreamPanel({ caseKey, active, tickNow }) {
  const [entries, setEntries] = useState({});
  const [connectionState, setConnectionState] = useState("idle"); // idle | connecting | open | closed | error
  const [error, setError] = useState(null);
  const streamRef = useRef(null);

  useEffect(() => {
    if (!active || !caseKey) return undefined;
    setConnectionState("connecting");
    setEntries({});
    setError(null);

    const stream = connectStageStream(caseKey, {
      onOpen: () => setConnectionState("open"),
      onComplete: () => setConnectionState("closed"),
      onError: (err) => {
        setConnectionState("error");
        setError(err?.message || "Streaming endpoint error");
      },
      onEvent: (evt) => {
        if (evt.event === "started") return;
        if (evt.event === "complete") {
          setConnectionState("closed");
          return;
        }
        if (evt.event === "error") {
          setConnectionState("error");
          try {
            const payload = JSON.parse(evt.data || "{}");
            setError(payload.message || "Streaming endpoint error");
          } catch {
            setError("Streaming endpoint error");
          }
          return;
        }
        if (evt.event !== "stage") return;
        try {
          const payload = JSON.parse(evt.data);
          setEntries((prev) => ({
            ...prev,
            [payload.stage]: { ...(prev[payload.stage] || {}), ...payload },
          }));
        } catch {
          // Ignore malformed event; streaming is advisory.
        }
      },
    });
    if (!stream) {
      setConnectionState("idle");
      return undefined;
    }
    streamRef.current = stream;

    return () => {
      stream.close();
      streamRef.current = null;
      setConnectionState("closed");
    };
  }, [active, caseKey]);

  const connectionBadge = (() => {
    if (connectionState === "connecting") return { color: "blue", text: "Connecting…" };
    if (connectionState === "open") return { color: "green", text: "Live" };
    if (connectionState === "error") return { color: "red", text: "Error" };
    if (connectionState === "closed") return { color: "grey", text: "Closed" };
    return null;
  })();

  return (
    <div className="stream-panel">
      <div className="stream-panel-header">
        <span className="inline-icon-label">
          <Sparkles size={14} /> Agent reasoning (live stream)
        </span>
        {connectionBadge ? <Badge color={connectionBadge.color}>{connectionBadge.text}</Badge> : null}
      </div>
      <div className="stream-stages">
        {STREAM_STAGES.map((stage) => {
          const entry = entries[stage.key];
          const elapsedMs =
            entry?.status === "running" && entry?.started_at
              ? tickNow - Date.parse(entry.started_at)
              : null;
          return <StageCard key={stage.key} stage={stage} entry={entry} elapsedMs={elapsedMs} />;
        })}
      </div>
      {error ? (
        <Box color="text-status-error" fontSize="body-s">
          {error}
        </Box>
      ) : null}
    </div>
  );
}

function StepRow({ step, isActive, elapsedMsDuringActive, caseKey, tickNow }) {
  const { row, label, hint, concreteName } = step;
  const status = row?.status || (isActive ? "pending" : "waiting");
  let icon;
  let badgeText;
  let color;
  if (status === "exited") {
    icon = <CheckCircle2 size={20} color="#0f7d3b" />;
    badgeText = formatDuration(row?.duration_ms);
    color = "green";
  } else if (status === "failed") {
    icon = <XCircle size={20} color="#c02828" />;
    badgeText = "Failed";
    color = "red";
  } else if (status === "running") {
    icon = <Loader2 size={20} color="#0972d3" className="spin" />;
    badgeText = elapsedMsDuringActive != null ? `${formatDuration(elapsedMsDuringActive)} in flight` : "in flight";
    color = "blue";
  } else {
    icon = <Circle size={20} color="#8c8c8c" />;
    badgeText = "";
    color = "grey";
  }
  const concreteNote =
    concreteName && concreteName !== step.key ? (
      <Box display="inline" color="text-status-info" fontSize="body-s">
        &nbsp;→ {concreteName}
      </Box>
    ) : null;
  // Nested stage-stream panel: for "Invoke AgentCore" step, shown while it's
  // running AND after it completes (we keep it open so the audience can still
  // read the agent reasoning after the fact). Not shown on the deterministic-
  // escalation path where this step never runs.
  const showStream = step.key === "Invoke AgentCore" && (status === "running" || status === "exited");
  const showNormalizeBreak = step.key === "Normalize exception" && status === "exited" && row?.output?.normalization;
  return (
    <div className={`step-row step-${status}`}>
      <div className="step-icon">{icon}</div>
      <div className="step-body">
        <div className="step-label">
          <strong>{label}</strong>
          {concreteNote}
        </div>
        <div className="step-hint">{hint}</div>
        {showNormalizeBreak ? <NormalizeBreakPanel output={row.output} /> : null}
        {showStream ? <StageStreamPanel caseKey={caseKey} active={showStream} tickNow={tickNow} /> : null}
      </div>
      <div className="step-badge">{badgeText ? <Badge color={color}>{badgeText}</Badge> : null}</div>
    </div>
  );
}

export default function LiveExecutionPanel({ execution, onFinished }) {
  const [state, setState] = useState({ status: null, history: [], output: null, error: null });
  const [now, setNow] = useState(Date.now());
  const pollingRef = useRef(null);
  const tickRef = useRef(null);

  // Poll the describe endpoint every ~1.2s until the execution leaves RUNNING.
  useEffect(() => {
    if (!execution?.execution_arn) return undefined;
    let cancelled = false;
    async function poll() {
      try {
        const data = await describeExecution(execution.execution_arn);
        if (cancelled) return;
        setState({
          status: data.status,
          history: data.history || [],
          output: data.output,
          error: data.error ? { error: data.error, cause: data.cause } : null,
        });
        if (data.status && data.status !== "RUNNING") {
          if (pollingRef.current) clearInterval(pollingRef.current);
          if (onFinished) onFinished(data);
        }
      } catch (err) {
        if (cancelled) return;
        setState((prev) => ({ ...prev, status: prev.status || "ERROR", error: { cause: err.message } }));
      }
    }
    poll();
    pollingRef.current = setInterval(poll, 1200);
    return () => {
      cancelled = true;
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [execution?.execution_arn, onFinished]);

  // Separate 250ms ticker just for the "elapsed" badge under the running step.
  useEffect(() => {
    tickRef.current = setInterval(() => setNow(Date.now()), 250);
    return () => {
      if (tickRef.current) clearInterval(tickRef.current);
    };
  }, []);

  const rows = useMemo(() => pickTimelineRows(state.history), [state.history]);
  const runningRow = rows.find((r) => r.row?.status === "running");
  const runningElapsedMs = runningRow?.row?.entered_at ? now - Date.parse(runningRow.row.entered_at) : null;

  const headerStatus = (() => {
    if (!state.status) return { type: "in-progress", text: "Starting execution…" };
    if (state.status === "RUNNING") return { type: "in-progress", text: "Live triage in progress" };
    if (state.status === "SUCCEEDED") return { type: "success", text: "Triage complete" };
    if (state.status === "FAILED") return { type: "error", text: "Execution failed" };
    return { type: "warning", text: state.status };
  })();

  // Overall progress = fraction of visible steps that have exited.
  const exitedCount = rows.filter((r) => r.row?.status === "exited").length;
  const percent = Math.round((exitedCount / rows.length) * 100);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={`Case ${execution?.case_key} — execution ${execution?.execution_name || ""}`}
        >
          Live triage progress
        </Header>
      }
    >
      <SpaceBetween size="m">
        <StatusIndicator type={headerStatus.type}>{headerStatus.text}</StatusIndicator>
        <ProgressBar value={percent} description={`${exitedCount} of ${rows.length} steps complete`} />
        <div className="step-list">
          {rows.map((step) => (
            <StepRow
              key={step.key}
              step={step}
              isActive={step === runningRow}
              elapsedMsDuringActive={step === runningRow ? runningElapsedMs : null}
              caseKey={execution?.case_key}
              tickNow={now}
            />
          ))}
        </div>
        {state.error ? (
          <Box color="text-status-error">
            <strong>Error:</strong> {state.error.cause || state.error.error}
          </Box>
        ) : null}
      </SpaceBetween>
    </Container>
  );
}
