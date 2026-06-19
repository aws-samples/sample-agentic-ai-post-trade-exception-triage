# Security

## Synthetic Data Only

The sample must never include real customer, counterparty, account, security, trade, settlement, or operational data.

Use only fictional identifiers such as:

- `CP-SYN-4421`
- `ACCT-SYN-8812`
- `SYNTH-EQ-001`
- `CASE-SYN-7781`

## Agent Authority

Agents are advisory only. They may:

- Summarize exception context.
- Retrieve permitted evidence.
- Map playbooks.
- Recommend next actions.

Agents may not:

- Resolve cases.
- Update settlement instructions.
- Modify enterprise records.
- Override eligibility rules.
- Override policy decisions.
- Bypass human approval.

## Gateway Tool Access

AgentCore Gateway tools must be read-only in this sample.

Allowed tools:

- `get_trade_details`
- `get_settlement_status`
- `get_allocation_status`
- `get_ssi_record`
- `search_prior_cases`
- `get_playbook`

No write-capable Gateway tools are allowed in the sample.

The AgentCore Gateway client only accepts HTTPS Gateway URLs before constructing outbound requests. This prevents local file or custom URL schemes from being passed to `urllib.request.urlopen`.

Every Gateway tool request is case-scoped. The tool Lambda requires `exception_id` on each tool call, validates synthetic identifier formats, rejects unexpected arguments, and verifies that counterparty, account, root cause, and playbook inputs match the active exception. This control is enforced in code, outside prompts.

## Policy Posture

AgentCore Policy should use a default-deny posture.

Policies should demonstrate:

- Agent-specific tool access.
- Operation-level access.
- Parameter-level checks.
- Product, desk, market, or severity constraints.
- Denial for restricted counterparties or unsupported cases.

Authorization must be enforced outside prompts.

The sample keeps both layers: AgentCore Policy enforces the deployed Gateway authorization envelope, and the Gateway tool Lambda enforces case-scoped parameter checks before reading synthetic evidence.

## IAM

Use least privilege for:

- Lambda execution roles.
- Step Functions role.
- AgentCore Runtime role.
- Gateway tool handler role.
- UI API role.
- Evaluation role.

Avoid wildcard permissions unless required for a documented service limitation. The sample scopes AgentCore Runtime invocation to the generated Runtime ARN and its `DEFAULT` runtime endpoint ARN. Remaining wildcard resources are limited to service APIs that require `Resource: "*"`, AgentCore creation-order constraints, or documented sample trade-offs listed below.

## Supply Chain

Direct Python and frontend dependencies are pinned in source manifests. CI installs pinned Python dependencies, runs `pip-audit` against `requirements.txt`, `requirements.runtime.txt`, and `requirements.streaming.txt`, builds the frontend with `npm ci`, and runs `npm audit --audit-level=moderate`.

## Sample Security Trade-offs

This sample is designed for reproducible deployment, not as a production security baseline. The following trade-offs must be reviewed before adapting the pattern to a non-sample account:

| # | Trade-off | Where it lives | Why | Production hardening |
|---|-----------|----------------|-----------------|---------------------|
| 1 | Gateway role `bedrock-agentcore:GetPolicyEngine` on `policy-engine/*` | `infra/triage_stack.py` Gateway role `PolicyEngineConfiguration` statement | Gateway creation needs Policy Engine lookup before the specific post-deploy attachment is complete. | Scope to the specific Policy Engine ARN after first deploy where service ordering permits it. |
| 2 | Gateway role `AuthorizeAction` and `PartiallyAuthorizeActions` on `policy-engine/*` and `gateway/*` | Same role, `PolicyEngineAuthorization` statement | AgentCore evaluates authorization across the attached engine and gateway. The sample keeps first-deploy wildcards inside the deployed account and Region because the IDs are generated during creation. | Scope to the specific Policy Engine and Gateway ARNs after first deploy where service ordering permits it. |
| 3 | Runtime role X-Ray and CloudWatch metric actions use `Resource: "*"` | `infra/triage_stack.py` `AgentCoreRuntimeRole` | CloudWatch `PutMetricData` and several X-Ray APIs do not support practical resource-level scoping. The CloudWatch metric statement is constrained to the `bedrock-agentcore` namespace. | Keep service-required wildcard resources; use conditions where supported. |
| 4 | Runtime role Bedrock model access uses the configured inference profile ARN plus a Region-scoped foundation-model ARN pattern | `infra/triage_stack.py` `AgentCoreRuntimeRole` | Bedrock inference profiles require both the profile ARN and the foundation models associated with the profile. The sample derives a pattern from `BEDROCK_MODEL_ID` and keeps it pinned to the deployed Region. | Replace the foundation-model pattern with the exact model ARNs returned by `get-inference-profile` in controlled production deployments. |
| 5 | Runtime role CDK staging-key decrypt is added only when `CDK_BOOTSTRAP_KMS_KEY_ARN` is set | `infra/triage_stack.py` `AgentCoreRuntimeRole`; `scripts/deploy.sh` preflight | Default CDK bootstrap buckets do not need extra KMS permissions. If the `CDKToolkit` `FileAssetsBucket` uses SSE-KMS, the Runtime needs the exact key ARN to read its code ZIP. The deploy script fails closed for KMS-encrypted bootstrap buckets when the ARN is missing. | Keep `CDK_BOOTSTRAP_KMS_KEY_ARN` set to the exact bootstrap key ARN in accounts that use SSE-KMS bootstrap assets. |
| 6 | Runtime role Workload Identity on `workload-identity/<generated-runtime-name>-*` | Same role | The exact workload-identity ARN is assigned by AgentCore at runtime creation. | Specific workload-identity ARN via stack output. |
| 7 | UI API Lambda runs under a dedicated `UiApiRole` rather than the shared task role | `infra/triage_stack.py` UI API section | Attaching `states:StartExecution` to the shared task role creates a CloudFormation circular dependency. | Keep the role split and attach only UI-required API permissions. |
| 8 | Browser demo UI/API uses mandatory demo Cognito protection, not production tenant authorization | `frontend/` and `infra/triage_stack.py` | Every deployment creates Cognito Hosted UI and an API Gateway Cognito authorizer. Public self-registration and the user-password auth flow are disabled. The API also validates token audience against the generated app client. The stack applies API Gateway throttling, a regional AWS WAF web ACL with AWS managed rules and a per-IP rate rule, CloudFront-only CORS, Lambda reserved concurrency, and an execution-volume alarm. | Create or invite demo users through Cognito before sharing the protected UI. Add enterprise identity, tenant authorization, network controls, organization-specific WAF rules, audit requirements, and environment-specific authorization before production use. |
| 9 | AgentCore Runtime process binds to `0.0.0.0:8080` | `src/agentcore_runtime/app.py` local `__main__` entrypoint | AgentCore Runtime's HTTP service contract uses port 8080, and the managed hosting layer must reach the process inside the runtime environment. The exposed service endpoint is AgentCore-managed and invoked through AWS authorization, not a public unauthenticated listener. | Keep Runtime behind AgentCore. Do not run this development entrypoint on an untrusted host without host firewall controls. |

AgentCore Policy is part of the normal deployment path: `scripts/setup-demo.sh` runs policy configuration and verification automatically. For targeted reruns, `scripts/configure-policy.sh` creates or updates Policy Engine-scoped Cedar `permit` and `forbid` policies and switches the Gateway to `ENFORCE`; `scripts/verify-policy.sh` checks both an allowed and denied tool call. `setup-demo.sh` fails closed if `SKIP_POLICY` or `SKIP_VERIFY` is used without `I_UNDERSTAND_UNENFORCED_POLICY=1`.

The Gateway tool Lambda mirrors the most important Cedar deny in code: restricted synthetic counterparties cannot read SSI records even if the post-deploy Policy Engine path is incomplete. `get_ssi_record` requires `exception_id`, `counterparty_id`, and `account_id`, and the runtime Gateway client fails closed by default unless a deployed Gateway URL or identifier is configured. Unit tests opt into local Gateway mode explicitly with `AGENTCORE_GATEWAY_LOCAL_MODE=1`.

The shared task Lambda role does not have AgentCore Runtime invocation permission; only the AgentCore invocation Lambda and optional streaming Lambda can invoke the generated Runtime ARN and its `DEFAULT` runtime endpoint ARN. Every remaining trade-off that triggers cdk-nag is suppressed with a reason in `infra/nag_suppressions.py`. For customers adapting this sample, narrow these permissions where their production deployment ordering allows it and attach your organization's identity, tenant authorization, network, logging, and change-management controls.

## Encryption

Use KMS encryption where practical:

- DynamoDB synthetic data and audit records.
- S3 evaluation artifacts.

The sample uses separate customer-managed keys for synthetic data/artifacts and AgentCore Gateway/Policy Engine encryption. AgentCore Policy Engine customer-managed KMS still requires the documented Forward Access Session grant pattern; keeping it on a separate key prevents those AgentCore-specific grants from applying to DynamoDB or artifact data.

The S3 static UI bucket uses SSE-S3 and is private behind CloudFront OAC. This avoids the initial-deploy circular dependency and wildcard KMS key-policy condition that CDK must synthesize for a KMS-encrypted S3 origin with OAC. The UI bucket contains only the public SPA build and generated runtime config; do not place operational data in it.

## Audit And Monitoring

The sample should emit:

- Step Functions execution history.
- CloudWatch logs.
- CloudWatch metrics.
- Agent invocation metadata.
- Tool call outcomes.
- Policy decision summaries.
- Audit records in DynamoDB.

Audit records include the Runtime model ID, the real Step Functions execution ARN, AgentCore Runtime trace metadata when available, recommendation evidence references, evidence source IDs, playbook ID, recommended queue, validation decision, routing decision, and policy decisions. The audit writer uses a dedicated Lambda role and `attribute_not_exists(PK) AND attribute_not_exists(SK)` on `PutItem` to avoid accidental overwrites.

CloudTrail should capture AWS API activity for deployed resources.

## Demo UI Access

The Cloudscape UI/API is always protected by Amazon Cognito Hosted UI and an API Gateway Cognito authorizer. Public self-registration is disabled, so create or invite demo users through Cognito before sharing the protected UI. The stack also applies API Gateway stage throttling, a regional AWS WAF web ACL with `AWSManagedRulesCommonRuleSet`, `AWSManagedRulesAmazonIpReputationList`, a `1000` requests per five-minute per-IP rate rule, WAF logging with `Authorization` header redaction, CloudFront-only CORS, a CloudFront Content-Security-Policy, Lambda reserved concurrency, and a CloudWatch execution-volume alarm to reduce accidental abuse and cost spikes.

Cognito in this sample is a demo protection layer against unauthenticated casual use; it is not a tenant authorization model. Production adaptations must add enterprise identity, tenant/user authorization, network controls, and stronger abuse protections before exposing operational APIs.

## Bedrock Guardrails

The stack creates an Amazon Bedrock Guardrail and passes the generated guardrail ID and version to the Strands `BedrockModel` call. The sample guardrail enables content filters, prompt-attack filtering, and PII redaction/blocking suitable for synthetic walkthroughs. Production adaptations should tune denied topics, sensitive-information policies, contextual grounding, and escalation handling to the institution's data-classification and model-risk-management requirements.
