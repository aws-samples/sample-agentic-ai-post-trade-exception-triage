# Evaluation

## Purpose

Evaluation proves that the agent-assisted triage flow produces useful, structured, and bounded recommendations. It should measure both agent quality and control behavior.

The deployed sample has two evaluation modes in practice:

- **Model-backed**: Bedrock model access is enabled and the Strands stages receive model output.
- **Deterministic fallback**: model calls are disabled or unavailable, and the deterministic stage logic produces the same schema-compatible output used by local tests.

Use model-backed runs for external claims about model behavior. Use fallback-backed runs to validate the control layer, policy behavior, schema shape, routing, and reproducibility of the synthetic sample.

## Golden Dataset

The golden dataset is synthetic. It should contain expected outputs for each required case:

- Expected root cause category.
- Expected playbook ID.
- Required evidence references.
- Expected queue.
- Expected escalation behavior.
- Expected human approval requirement.

Required cases:

- `missing_ssi`
- `unmatched_allocation`
- `counterparty_mismatch`
- `settlement_status_mismatch`
- `missing_confirmation`
- `restricted_counterparty`
- `near_cutoff_escalation`
- `stale_reference_data`

## Metrics

Required metrics:

| Metric | Meaning |
| --- | --- |
| Playbook accuracy | Percent of cases where the selected playbook matches the golden dataset. |
| Evidence recall | Percent of required evidence references retrieved by the agent. |
| Escalation correctness | Percent of cases where escalation behavior matches the expected result. |
| Policy-denial correctness | Percent of cases where expected policy denials occur before a disallowed tool result is used. |
| Unauthorized tool attempt rate | Percent of evaluated cases where the workflow attempted a tool call outside the intended read-only policy envelope. |
| Invalid output rate | Percent of agent responses rejected by schema or validation rules. |
| Latency | Workflow and agent runtime duration, including p50 and p95 where available. |

## Managed Evaluation

Use Amazon Bedrock AgentCore Evaluations where supported for the deployed sample. The evaluation flow should:

1. Load the golden dataset from S3.
2. Run each synthetic case through the deployed agent-assisted triage flow or a controlled evaluation entrypoint.
3. Compare actual recommendation output against expected fields.
4. Store evaluation output in S3.
5. Write summary metrics to DynamoDB or CloudWatch.
6. Display metrics in the Cloudscape UI.

The agent implementation should use Strands Agents so traces and spans can align with AgentCore Evaluations support for Strands-based agents.

## Acceptance Thresholds

For the sample to be considered demo-ready:

- Playbook accuracy: at least 85%.
- Evidence recall: at least 85%.
- Escalation correctness: 100% for restricted and near-cutoff cases.
- Invalid output rate: 0% for the seeded golden dataset.
- All evaluation outputs must be reproducible from `./scripts/run-evaluation.sh`.

These thresholds are for the sample only. They are not production recommendations.

## Evaluation Reporting

`run-evaluation.sh` should print:

- Evaluation run ID.
- Number of cases evaluated.
- Metric summary.
- Artifact S3 path.
- Cloudscape deep link.
