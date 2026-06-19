# Design

## AWS-Deployed Mode Only

The sample intentionally does not provide a local-only mode. Customers should see the deployed AWS control path, Step Functions execution history, AgentCore Runtime behavior, Gateway tool access, Policy enforcement, CloudWatch telemetry, and Cloudscape UI.

## Step Functions Standard Workflow

Use AWS Step Functions Standard Workflow for the agent-assisted triage control layer.

Rationale:

- Each exception is a business case that benefits from durable execution history.
- The workflow contains explicit control points before and after agent invocation.
- The sample needs readable execution history for demos and auditability.
- Retries, timeouts, fallback paths, and failure states are part of the pattern.

Do not use Express Workflow for the first release. Express is better for very high-volume, short-lived event processing where durable per-case history is less important.

## One AgentCore Runtime

Use one AgentCore Runtime that hosts one Python agent app implemented with Strands Agents. The app contains four Strands-based internal stages:

- Summary.
- Evidence retrieval.
- Playbook mapping.
- Recommendation.

This design avoids operational overhead from deploying multiple runtimes while preserving the conceptual four-agent model from the blog post.

## Strands Agents Framework

Use Strands Agents as the agent framework inside AgentCore Runtime.

Rationale:

- AgentCore Runtime supports Strands Agents.
- AgentCore Evaluations supports Strands Agents and LangGraph instrumentation patterns.
- Strands keeps the sample Python-first, readable, and suitable for `aws-samples`.
- Strands can model the four internal stages without adding unnecessary graph orchestration complexity.
- Strands integrates naturally with Amazon Bedrock model invocation and MCP/Gateway tool access patterns.

Do not use LangGraph, CrewAI, or a custom-only agent framework unless Strands is blocked by a current SDK or runtime limitation. If blocked, document the blocker in `docs/HANDOFF.md` before using a fallback.

Required internal module shape:

```text
src/agentcore_runtime/
  app.py
  triage_workflow.py
  agents/
    summary_agent.py
    evidence_agent.py
    playbook_agent.py
    recommendation_agent.py
```

Each stage should expose a small typed function so deterministic tests can exercise the agent workflow without depending on UI code.

## Lambda Tasks For Deterministic Logic

Step Functions owns the control flow. Lambda functions implement deterministic tasks:

- Normalize exception.
- Calculate scope and severity.
- Invoke AgentCore.
- Validate agent output.
- Route enriched case.
- Send manual triage.
- Escalate.
- Record audit state.

The state machine should remain readable and should not hide all logic inside one opaque service.

## AgentCore Gateway Read-Only Tools

Gateway tools are read-only by default. This demonstrates controlled evidence assembly without granting agents direct authority over case state or operational systems.

Write actions are out of scope for the first release. If a future version adds write-capable tools, they must require explicit human approval and deterministic validation.

## Policy Outside Prompts

AgentCore Policy enforces tool and parameter authorization outside prompts. The prompt may describe allowed behavior, but only Policy and deterministic workflow gates decide whether an action is allowed.

Policy examples should demonstrate:

- Default-deny posture.
- Agent-specific tool permissions.
- Desk or product matching.
- Case severity constraints.
- No write-capable operations.

## Human Approval By Default

The sample should always treat operationally meaningful changes as requiring human approval. Recommendation output is advisory. The enriched case package can be routed to the simulated analyst queue, but the sample must not auto-resolve an exception.

## Cloudscape UI

Cloudscape is the visual "wow" experience for the first release. It should feel like an AWS-native operational console, not a marketing page.

Required UI views:

- Case selector.
- Execution timeline.
- Evidence graph.
- Recommendation card.
- Policy decisions.
- Confidence score.
- Evaluation metrics.
- Deep links to Step Functions execution and CloudWatch dashboard.

## QuickSight Deferred

QuickSight is intentionally deferred. The first release uses CloudWatch dashboard and Cloudscape UI for visual metrics. QuickSight can be added later if the target account already has QuickSight enabled.
