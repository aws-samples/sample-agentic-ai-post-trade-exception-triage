# Requirements

## Purpose

Build an AWS-deployed sample for `aws-samples` that demonstrates the agent-assisted post-trade exception triage pattern described in the blog post "Agentic post-trade exception triage with Amazon Bedrock AgentCore."

The blog post is the functional source of truth. The repository implements the agent-assisted triage path from the post:

1. Receive a synthetic post-trade exception event.
2. Apply deterministic eligibility, severity, validation, routing, and escalation gates.
3. Invoke Amazon Bedrock AgentCore only when the case is eligible for agentic enrichment.
4. Run four specialized agent stages: summary, evidence retrieval, playbook mapping, and recommendation.
5. Retrieve permitted evidence only through governed read-only tools exposed by Amazon Bedrock AgentCore Gateway.
6. Enforce tool, operation, and parameter controls outside prompts with Amazon Bedrock AgentCore Policy.
7. Validate the agent recommendation before it can influence the operational workflow.
8. Return an enriched case package to the simulated analyst workflow.
9. Record audit state and evaluate quality against a synthetic golden dataset.

## Fixed Decisions

| Decision | Value |
| --- | --- |
| AWS Region | Configurable through `AWS_REGION`; scripts default to `us-east-1` |
| AWS profile | Optional; use `AWS_PROFILE=<your-profile>` or the default credential chain |
| Runtime Bedrock model | Claude Opus 4.6 geo inference profile by default: `us.anthropic.claude-opus-4-6-v1`; override with `BEDROCK_MODEL_ID` |
| Evaluator Bedrock model | Lower-cost AgentCore Evaluator judge model by default: `us.anthropic.claude-haiku-4-5-20251001-v1:0`; override with `BEDROCK_EVALUATOR_MODEL_ID` |
| Agent framework | Strands Agents |
| Runtime mode | AWS deployed mode only |
| QuickSight | Deferred |
| Data | 100% synthetic and fictional |
| Visual experience | Cloudscape UI, Step Functions execution view, CloudWatch dashboard |
| Repo name | `agentic-post-trade-exception-triage` |
| Documentation language | English |

## Scope

The sample demonstrates an agent-assisted triage control layer. It does not replace existing clearing, settlement, reconciliation, or case-management systems.

In the sample, existing post-trade systems are simulated with synthetic datasets stored in AWS. In a real deployment, those systems would remain systems of record and would integrate with the agent-assisted layer through approved APIs, events, and workflow integration points.

## Success Criteria

A user can clone the repository and run:

```bash
export AWS_REGION=us-east-1

./scripts/setup-demo.sh
./scripts/run-case.sh missing_ssi
./scripts/run-evaluation.sh
./scripts/destroy.sh
```

The deployed sample must show:

- A Step Functions Standard Workflow execution for the agent-assisted triage control layer.
- An AgentCore Runtime invocation that produces a structured advisory recommendation.
- Strands Agents running inside AgentCore Runtime for the summary, evidence retrieval, playbook mapping, and recommendation stages.
- AgentCore Gateway tools that expose read-only synthetic evidence.
- AgentCore Policy controls for tool, operation, and parameter-level authorization.
- AgentCore Evaluations or an evaluation integration that reports quality metrics from a golden dataset.
- Cloudscape UI showing the case timeline, evidence, recommendation, policy decisions, confidence, and evaluation results.
- CloudWatch dashboard showing workflow and agent runtime operational signals.

## Traceability To Blog Post

| Blog concept | Sample implementation |
| --- | --- |
| Solution overview | Step Functions Standard Workflow implements the agent-assisted triage control layer. |
| Four specialized agents | One AgentCore Runtime hosts Strands-based internal summary, evidence retrieval, playbook mapping, and recommendation stages. |
| Governed evidence access | AgentCore Gateway exposes read-only tools backed by synthetic data. |
| Policy outside prompts | AgentCore Policy governs tool, operation, and parameter access. |
| Observability and auditability | CloudWatch, audit records, Step Functions execution history, and structured telemetry. |
| Evaluation before and after production | AgentCore Evaluations plus synthetic golden dataset. |
| Progressive rollout | Read-only tools by default, human approval by default, write actions out of scope. |

## Non-Goals

- Do not implement a replacement clearing, settlement, reconciliation, or case-management platform.
- Do not connect to real post-trade systems.
- Do not include real counterparties, accounts, securities, customers, or trade records.
- Do not enable autonomous write-back to operational systems.
- Do not require QuickSight in the first release.
- Do not make the blog post a deployment walkthrough; keep operational details in this repository.
