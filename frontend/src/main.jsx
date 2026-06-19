import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "@cloudscape-design/global-styles/index.css";
import {
  Alert,
  AppLayout,
  Badge,
  Box,
  Button,
  Cards,
  ColumnLayout,
  Container,
  ContentLayout,
  Header,
  Link,
  ProgressBar,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
} from "@cloudscape-design/components";
import { AlertTriangle, Gauge, GitBranch, ShieldCheck, Sparkles } from "lucide-react";
import LiveExecutionPanel from "./liveExecution";
import { fetchEvaluationMetrics, fetchGoldenCases, startExecution } from "./api";
import { completeHostedUiSignIn, isAuthEnabled, isSignedIn, signIn, signOut } from "./auth";
import "./styles.css";

const runtimeConfig = typeof window !== "undefined" ? window.__TRIAGE_CONFIG || {} : {};
const AWS_REGION = runtimeConfig.awsRegion || "us-east-1";
const DASHBOARD_NAME = runtimeConfig.dashboardName || "agentic-post-trade-triage";
const STEP_CONSOLE = `https://${AWS_REGION}.console.aws.amazon.com/states/home?region=${AWS_REGION}#/statemachines`;
const CLOUDWATCH_URL = `https://${AWS_REGION}.console.aws.amazon.com/cloudwatch/home?region=${AWS_REGION}#dashboards:name=${encodeURIComponent(DASHBOARD_NAME)}`;

function priorityColor(priority) {
  switch ((priority || "").toUpperCase()) {
    case "CRITICAL":
      return "red";
    case "HIGH":
      return "red";
    case "MEDIUM":
      return "blue";
    case "LOW":
      return "grey";
    default:
      return "grey";
  }
}

function CaseCardGrid({ cases, activeCaseKey, onRun, disabled }) {
  return (
    <Cards
      cardDefinition={{
        header: (item) => (
          <SpaceBetween direction="horizontal" size="xs">
            <span>{item.title}</span>
            {item.will_escalate_deterministically ? (
              <Badge color="red">
                <span className="inline-icon-label inline-icon-label-xs">
                  <AlertTriangle size={12} /> rule-based escalation
                </span>
              </Badge>
            ) : null}
          </SpaceBetween>
        ),
        sections: [
          {
            id: "body",
            content: (item) => (
              // One flex-column wrapper that owns the whole card body. The
              // summary takes flex: 1 so it absorbs any height difference
              // between cards in the same row — keeping the Run triage
              // button aligned at the bottom-left of every card regardless
              // of how long the summary is.
              <div className="case-card-body">
                <Box variant="p">{item.summary}</Box>
                <div className="case-card-meta">
                  <div>
                    <Box variant="awsui-key-label">Exception</Box>
                    <Box fontWeight="bold">{item.exception_id}</Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">Priority</Box>
                    <Badge color={priorityColor(item.priority)}>{item.priority || "—"}</Badge>
                  </div>
                </div>
                <div className="case-card-actions">
                  <Button
                    variant="primary"
                    iconName="caret-right-filled"
                    disabled={disabled}
                    loading={activeCaseKey === item.case_key && disabled}
                    onClick={() => onRun(item)}
                  >
                    {activeCaseKey === item.case_key && disabled ? "Running…" : "Run triage"}
                  </Button>
                </div>
              </div>
            ),
          },
        ],
      }}
      cardsPerRow={[{ cards: 1 }, { minWidth: 640, cards: 2 }, { minWidth: 1024, cards: 4 }]}
      items={cases}
      trackBy="case_key"
      empty={
        <Box textAlign="center" color="text-body-secondary">
          No cases available.
        </Box>
      }
    />
  );
}

function firstTextValue(item, keys) {
  for (const key of keys) {
    const value = item?.[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
    if (typeof value === "number") {
      return String(value);
    }
  }
  return "";
}

function normalizeKeyEvidenceRows(items, evidenceRefs = []) {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item, index) => {
      if (typeof item === "string" && item.trim()) {
        return {
          row_id: `text-${index}-${item}`,
          label: "Evidence",
          value: item.trim(),
          source_ref: evidenceRefs[index] || "—",
        };
      }
      if (!item || typeof item !== "object") {
        return null;
      }
      const label =
        firstTextValue(item, ["label", "evidence", "type", "name", "title", "field"]) || "Evidence";
      const value = firstTextValue(item, [
        "value",
        "signal",
        "summary",
        "detail",
        "details",
        "rationale",
        "reason",
        "description",
      ]);
      const sourceRef =
        firstTextValue(item, ["source_ref", "sourceRef", "source_id", "sourceId", "source", "ref", "reference"]) ||
        evidenceRefs[index] ||
        "—";
      if (!value && sourceRef === "—" && label === "Evidence") {
        return null;
      }
      return {
        row_id: `${index}-${label}-${sourceRef}-${value}`,
        label,
        value: value || "—",
        source_ref: sourceRef,
      };
    })
    .filter(Boolean);
}

function RecommendationPanel({ output, caseKey }) {
  if (!output) {
    return (
      <Container header={<Header variant="h2">Recommendation</Header>}>
        <Box color="text-body-secondary">
          The recommendation will appear here once the execution finishes.
        </Box>
      </Container>
    );
  }
  const recommendation = output?.agent?.recommendation;
  const finalStatus = output?.final_status;
  const routing = output?.routing || {};
  const validation = output?.validation || {};
  const gate = output?.gate || {};
  const trace = output?.agent?.trace || {};
  if (!recommendation) {
    // Deterministic escalation path — agent never ran. Make it crystal-clear
    // for non-technical reviewers why there is no recommendation.
    return (
      <Container
        header={
          <Header variant="h2" description={`Case ${caseKey}`}>
            Deterministic escalation
          </Header>
        }
      >
        <SpaceBetween size="s">
          <StatusIndicator type="warning">
            {finalStatus === "ESCALATED" ? "Escalated without invoking the agent" : finalStatus || "Escalated"}
          </StatusIndicator>
          <Box variant="p">
            The deterministic control layer escalated this case before calling Claude Opus 4.6. This is the
            sample's guardrail in action: policy takes precedence over agent reasoning, always.
          </Box>
          {routing.reason ? (
            <Box>
              <Box variant="awsui-key-label">Reason</Box>
              <Box>{routing.reason}</Box>
            </Box>
          ) : null}
          {routing.queue ? (
            <Box>
              <Box variant="awsui-key-label">Routed to</Box>
              <Badge color="red">{routing.queue}</Badge>
            </Box>
          ) : null}
        </SpaceBetween>
      </Container>
    );
  }
  const confidencePct = Math.round((Number(recommendation.confidence) || 0) * 100);
  const isEscalation =
    finalStatus === "ESCALATED" ||
    (recommendation.root_cause_category || "").includes("POLICY_OR_CUTOFF_ESCALATION");
  const keyEvidence = normalizeKeyEvidenceRows(recommendation.key_evidence, recommendation.evidence_refs);
  const nextSteps = Array.isArray(recommendation.recommended_next_steps)
    ? recommendation.recommended_next_steps
    : [];
  const openQuestions = Array.isArray(recommendation.open_questions) ? recommendation.open_questions : [];
  const riskFlags = Array.isArray(recommendation.risk_flags) ? recommendation.risk_flags : [];
  return (
    <Container
      header={
        <Header
          variant="h2"
          description={`Case ${caseKey} — ${finalStatus || "complete"}`}
          actions={
            <Badge color={isEscalation ? "red" : "blue"}>
              {recommendation.recommended_queue || routing.queue || "—"}
            </Badge>
          }
        >
          <span className="inline-icon-label">
            <Sparkles size={18} /> Recommendation
          </span>
        </Header>
      }
    >
      <SpaceBetween size="m">
        <StatusIndicator type={isEscalation ? "warning" : "success"}>
          {isEscalation
            ? "Escalate for urgent manual review"
            : `Route enriched case to ${recommendation.recommended_queue || "analyst queue"}`}
        </StatusIndicator>

        <Box variant="p">{recommendation.recommended_action}</Box>

        {recommendation.analyst_summary || recommendation.decision_rationale ? (
          <ColumnLayout columns={2} variant="text-grid">
            {recommendation.analyst_summary ? (
              <div>
                <Box variant="awsui-key-label">Analyst brief</Box>
                <Box variant="p">{recommendation.analyst_summary}</Box>
              </div>
            ) : null}
            {recommendation.decision_rationale ? (
              <div>
                <Box variant="awsui-key-label">Why this route</Box>
                <Box variant="p">{recommendation.decision_rationale}</Box>
              </div>
            ) : null}
          </ColumnLayout>
        ) : null}

        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Root cause</Box>
            <Badge>{recommendation.root_cause_category}</Badge>
          </div>
          <div>
            <Box variant="awsui-key-label">Playbook</Box>
            <Box>{recommendation.playbook_id}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Human approval</Box>
            <Badge color={recommendation.human_approval_required ? "blue" : "red"}>
              {recommendation.human_approval_required ? "Required" : "Bypassed"}
            </Badge>
          </div>
          {recommendation.suggested_sla_minutes ? (
            <div>
              <Box variant="awsui-key-label">Suggested SLA</Box>
              <Badge color={recommendation.suggested_sla_minutes <= 20 ? "red" : "blue"}>
                {recommendation.suggested_sla_minutes} min
              </Badge>
            </div>
          ) : null}
        </ColumnLayout>

        {riskFlags.length ? (
          <div>
            <Box variant="awsui-key-label">Risk flags</Box>
            <SpaceBetween direction="horizontal" size="xs">
              {riskFlags.map((flag) => (
                <Badge key={flag} color={flag.includes("HIGH") || flag.includes("cut-off") ? "red" : "blue"}>
                  {flag}
                </Badge>
              ))}
            </SpaceBetween>
          </div>
        ) : null}

        {nextSteps.length || openQuestions.length ? (
          <ColumnLayout columns={2}>
            {nextSteps.length ? (
              <Container header={<Header variant="h3">Next best actions</Header>}>
                <ol className="recommendation-list">
                  {nextSteps.map((step) => (
                    <li key={step}>{step}</li>
                  ))}
                </ol>
              </Container>
            ) : null}
            {openQuestions.length ? (
              <Container header={<Header variant="h3">Questions to resolve</Header>}>
                <ul className="recommendation-list">
                  {openQuestions.map((question) => (
                    <li key={question}>{question}</li>
                  ))}
                </ul>
              </Container>
            ) : null}
          </ColumnLayout>
        ) : null}

        {keyEvidence.length ? (
          <Table
            variant="embedded"
            header={<Header variant="h3">Key evidence behind the recommendation</Header>}
            columnDefinitions={[
              { id: "label", header: "Evidence", cell: (item) => item.label || "Evidence" },
              { id: "value", header: "Signal", cell: (item) => item.value || "—" },
              { id: "source", header: "Source ref", cell: (item) => item.source_ref || "—" },
            ]}
            items={keyEvidence}
            trackBy="row_id"
            wrapLines
          />
        ) : null}

        <div>
          <Box variant="awsui-key-label">
            <span className="inline-icon-label">
              <Gauge size={14} /> Confidence
            </span>
          </Box>
          <ProgressBar value={confidencePct} description={`${confidencePct}%`} />
        </div>

        {recommendation.policy_notes ? (
          <div>
            <Box variant="awsui-key-label">Policy notes</Box>
            <Box variant="p" color="text-body-secondary">
              {Array.isArray(recommendation.policy_notes)
                ? recommendation.policy_notes.join(" · ")
                : recommendation.policy_notes}
            </Box>
          </div>
        ) : null}

        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Validation</Box>
            <Badge color={validation.accepted ? "green" : "red"}>{validation.decision || "—"}</Badge>
          </div>
          <div>
            <Box variant="awsui-key-label">Gate decision</Box>
            <Box>{gate.decision || "—"}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Runtime latency</Box>
            <Box>{trace.latency_ms != null ? `${(trace.latency_ms / 1000).toFixed(1)} s` : "—"}</Box>
          </div>
        </ColumnLayout>
      </SpaceBetween>
    </Container>
  );
}

function EvidencePanel({ output }) {
  const evidence = output?.agent?.evidence || {};
  const items = Object.entries(evidence)
    .map(([key, value]) => ({ key, value }))
    .filter((item) => item.value && typeof item.value === "object");
  return (
    <Container
      header={
        <Header variant="h2">
          <span className="inline-icon-label">
            <GitBranch size={18} /> Evidence collected
          </span>
        </Header>
      }
    >
      {items.length === 0 ? (
        <Box color="text-body-secondary">
          No evidence yet. For deterministic-escalation cases no evidence is collected by design.
        </Box>
      ) : (
        <Cards
          cardDefinition={{
            header: (item) => item.key.replace(/_/g, " "),
            sections: [
              {
                id: "summary",
                content: (item) => (
                  <Box fontSize="body-s">
                    {Object.entries(item.value)
                      .slice(0, 4)
                      .map(([k, v]) => (
                        <div key={k}>
                          <Box variant="awsui-key-label">{k}</Box>
                          <Box>{String(v)}</Box>
                        </div>
                      ))}
                  </Box>
                ),
              },
            ],
          }}
          cardsPerRow={[{ cards: 1 }, { minWidth: 640, cards: 2 }, { minWidth: 1024, cards: 3 }]}
          items={items}
          trackBy="key"
        />
      )}
    </Container>
  );
}

function PolicyDecisionsPanel({ output }) {
  const decisions = output?.agent?.policy_decisions || [];
  return (
    <Container
      header={
        <Header variant="h2">
          <span className="inline-icon-label">
            <ShieldCheck size={18} /> Policy decisions
          </span>
        </Header>
      }
    >
      {decisions.length === 0 ? (
        <Box color="text-body-secondary">No policy decisions recorded.</Box>
      ) : (
        <Table
          variant="embedded"
          columnDefinitions={[
            { id: "tool", header: "Tool", cell: (item) => item.tool },
            {
              id: "decision",
              header: "Decision",
              cell: (item) => (
                <Badge color={item.decision === "ALLOW" ? "green" : "red"}>{item.decision}</Badge>
              ),
            },
            { id: "reason", header: "Reason", cell: (item) => item.reason || "" },
          ]}
          items={decisions}
        />
      )}
    </Container>
  );
}

function EvaluationMetrics({ metrics }) {
  if (!metrics || !metrics.case_count) {
    return (
      <Container header={<Header variant="h2">Evaluation metrics</Header>}>
        <Box color="text-body-secondary">
          Metrics will appear after the first evaluation run (
          <code>./scripts/run-evaluation.sh</code>).
        </Box>
      </Container>
    );
  }
  const pct = (v) => (typeof v === "number" ? `${Math.round(v * 1000) / 10}%` : "—");
  return (
    <Container
      header={
        <Header
          variant="h2"
          description={`Run ${metrics.evaluation_run_id || ""} · mode ${metrics.agent_invocation_mode || "—"}`}
        >
          Latest evaluation metrics
        </Header>
      }
    >
      <ColumnLayout columns={3} variant="text-grid">
        <Metric label="Playbook accuracy" value={pct(metrics.playbook_accuracy)} />
        <Metric label="Evidence recall" value={pct(metrics.evidence_recall)} />
        <Metric label="Escalation correctness" value={pct(metrics.escalation_correctness)} />
        <Metric label="Policy-denial correctness" value={pct(metrics.policy_denial_correctness)} />
        <Metric label="Invalid output rate" value={pct(metrics.invalid_output_rate)} />
        <Metric label="Unauthorized tool attempts" value={pct(metrics.unauthorized_tool_attempt_rate)} />
      </ColumnLayout>
    </Container>
  );
}

function Metric({ label, value }) {
  return (
    <Box>
      <Box variant="awsui-key-label">{label}</Box>
      <Box variant="h2">{value}</Box>
    </Box>
  );
}

function AuthExperienceItem({ icon, title, children }) {
  return (
    <div className="auth-experience-item">
      <div className="auth-experience-icon">{icon}</div>
      <div>
        <Box fontWeight="bold">{title}</Box>
        <Box color="text-body-secondary">{children}</Box>
      </div>
    </div>
  );
}

function AuthAccessPanel({ onSignIn }) {
  return (
    <Container>
      <div className="auth-access-shell">
        <div className="auth-access-copy">
          <Badge color="blue">
            <span className="inline-icon-label">
              <ShieldCheck size={14} /> Cognito protected demo
            </span>
          </Badge>
          <div className="auth-access-title">Explore governed post-trade exception triage</div>
          <div className="auth-access-description">
            Sign in with a provisioned demo user to try a synthetic operations workflow. You'll select a golden
            case, watch the AWS control path invoke AgentCore, inspect evidence and policy decisions, and finish
            with an analyst-ready recommendation. No customer data is used.
          </div>
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="primary" iconName="lock-private" onClick={onSignIn}>
              Sign in
            </Button>
          </SpaceBetween>
        </div>
        <div className="auth-experience-list" aria-label="Demo experience">
          <AuthExperienceItem icon={<GitBranch size={18} />} title="Choose a synthetic exception">
            Start from curated capital-markets golden cases, including missing SSI, stale reference data, and
            cutoff-sensitive breaks.
          </AuthExperienceItem>
          <AuthExperienceItem icon={<Sparkles size={18} />} title="Watch governed agent assistance">
            Follow the triage path as deterministic controls, AgentCore Runtime, Gateway tools, and Policy Engine
            work together.
          </AuthExperienceItem>
          <AuthExperienceItem icon={<Gauge size={18} />} title="Review analyst-ready output">
            Inspect evidence, routing, policy decisions, and evaluation metrics after authentication.
          </AuthExperienceItem>
        </div>
      </div>
    </Container>
  );
}

function App() {
  const [signedIn, setSignedIn] = useState(() => {
    completeHostedUiSignIn();
    return isSignedIn();
  });
  const [cases, setCases] = useState([]);
  const [metrics, setMetrics] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [execution, setExecution] = useState(null); // { execution_arn, case_key, ... }
  const [finalOutput, setFinalOutput] = useState(null);
  const [isRunning, setIsRunning] = useState(false);
  const [runError, setRunError] = useState(null);

  useEffect(() => {
    if (!isAuthEnabled() || !signedIn) return undefined;
    let cancelled = false;
    async function boot() {
      try {
        const [goldenCases, latest] = await Promise.all([
          fetchGoldenCases(),
          fetchEvaluationMetrics().catch(() => null),
        ]);
        if (cancelled) return;
        setCases(goldenCases);
        setMetrics(latest);
      } catch (err) {
        if (cancelled) return;
        setLoadError(err.message);
      }
    }
    boot();
    return () => {
      cancelled = true;
    };
  }, [signedIn]);

  const handleRun = useCallback(async (caseItem) => {
    setRunError(null);
    setFinalOutput(null);
    setIsRunning(true);
    try {
      const started = await startExecution(caseItem.case_key);
      setExecution({ ...started, case_key: caseItem.case_key, title: caseItem.title });
    } catch (err) {
      setIsRunning(false);
      setRunError(err.message);
    }
  }, []);

  const handleFinished = useCallback((data) => {
    setIsRunning(false);
    setFinalOutput(data.output || null);
  }, []);

  // When a new execution starts, smoothly scroll the Live triage progress
  // panel into view so the user isn't still looking at the case grid while
  // the state machine begins running. Keyed on the execution ARN so every
  // "Run triage" click re-scrolls (even when running the same case twice).
  const liveAnchorRef = useRef(null);
  useEffect(() => {
    if (!execution?.execution_arn) return;
    // Next tick, after the anchor has mounted.
    const id = window.requestAnimationFrame(() => {
      liveAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return () => window.cancelAnimationFrame(id);
  }, [execution?.execution_arn]);

  const activeCaseKey = execution?.case_key;
  const hasApi = typeof window !== "undefined" && window.__TRIAGE_CONFIG?.apiUrl;
  const authEnabled = isAuthEnabled();
  const authConfigMissing = hasApi && !authEnabled;
  const authRequired = hasApi && authEnabled && !signedIn;
  const canUseDemo = hasApi && authEnabled && signedIn;
  const showOperationalLinks = canUseDemo;

  return (
    <AppLayout
      navigationHide
      toolsHide
      content={
        <ContentLayout
          header={
            <Header
              variant="h1"
              description="AWS-deployed advisory triage over synthetic post-trade exceptions. All data is synthetic; existing post-trade systems remain conceptual systems of record."
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  {authEnabled && signedIn ? (
                    <Button
                      iconName="lock-private"
                      onClick={() => {
                        setSignedIn(false);
                        signOut();
                      }}
                    >
                      Sign out
                    </Button>
                  ) : null}
                  {showOperationalLinks ? (
                    <>
                      <Button iconName="external" href={STEP_CONSOLE} target="_blank">
                        Step Functions
                      </Button>
                      <Button iconName="external" href={CLOUDWATCH_URL} target="_blank">
                        CloudWatch
                      </Button>
                    </>
                  ) : null}
                </SpaceBetween>
              }
            >
              Agentic Post-Trade Exception Triage
            </Header>
          }
        >
          <SpaceBetween size="l">
            {!hasApi ? (
              <Alert type="warning" header="UI is not wired to an API">
                <code>/config.js</code> was not loaded; the UI can't talk to the stack. Re-deploy via{" "}
                <code>./scripts/deploy.sh</code>.
              </Alert>
            ) : null}
            {authConfigMissing ? (
              <Alert type="error" header="Authentication is not configured">
                <code>/config.js</code> is missing the Cognito Hosted UI settings required by this protected demo.
                Re-deploy via <code>./scripts/deploy.sh</code>.
              </Alert>
            ) : null}
            {loadError ? (
              <Alert type="error" header="Could not load case list">
                {loadError}
              </Alert>
            ) : null}
            {runError ? (
              <Alert type="error" header="Could not start the execution" dismissible onDismiss={() => setRunError(null)}>
                {runError}
              </Alert>
            ) : null}
            {authRequired ? (
              <AuthAccessPanel
                onSignIn={() => {
                  signIn();
                }}
              />
            ) : null}

            {canUseDemo ? (
              <Container
                header={
                  <Header
                    variant="h2"
                    description="Pick a case to see the four-stage agent-assisted triage flow run end-to-end. One case at a time."
                  >
                    Golden cases
                  </Header>
                }
              >
                {cases.length === 0 && !loadError ? (
                  <Box textAlign="center">
                    <Spinner /> Loading cases…
                  </Box>
                ) : (
                  <CaseCardGrid
                    cases={cases}
                    activeCaseKey={activeCaseKey}
                    onRun={handleRun}
                    disabled={isRunning}
                  />
                )}
              </Container>
            ) : null}

            {canUseDemo && execution ? (
              <div ref={liveAnchorRef} className="live-execution-anchor">
                <LiveExecutionPanel execution={execution} onFinished={handleFinished} />
              </div>
            ) : null}

            {canUseDemo && execution ? (
              <>
                <RecommendationPanel output={finalOutput} caseKey={execution.case_key} />
                <ColumnLayout columns={2}>
                  <EvidencePanel output={finalOutput} />
                  <PolicyDecisionsPanel output={finalOutput} />
                </ColumnLayout>
              </>
            ) : null}

            {canUseDemo ? <EvaluationMetrics metrics={metrics} /> : null}

            {canUseDemo ? (
              <Box color="text-body-secondary" textAlign="center">
                All records shown are synthetic. <Link external href={STEP_CONSOLE}>Inspect workflow</Link>
              </Box>
            ) : null}
          </SpaceBetween>
        </ContentLayout>
      }
    />
  );
}

createRoot(document.getElementById("root")).render(<App />);
