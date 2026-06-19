# Deployment

## Prerequisites

Required:

- AWS account with access to Amazon Bedrock and Amazon Bedrock AgentCore in the target Region. Scripts default to `us-east-1` unless you set `AWS_REGION`.
- Current AWS CLI v2 with `bedrock-agentcore-control` Policy Engine support.
- Bedrock model access for the configured `BEDROCK_MODEL_ID`. The default Runtime model is the Anthropic Claude Opus 4.6 geo inference profile `us.anthropic.claude-opus-4-6-v1`.
- AgentCore Evaluator authorization for the configured `BEDROCK_EVALUATOR_MODEL_ID`. The default LLM-as-judge model is `us.anthropic.claude-haiku-4-5-20251001-v1:0`. Evaluator creation is enabled by default and can fail if the evaluator model is unsupported in the target Region, model access is not authorized, or AWS Marketplace subscription actions are blocked for the provisioning role/account.
- Node.js 22 or 24 for CDK and Cloudscape UI build. Avoid odd-numbered non-LTS Node releases such as Node 23 because the CDK CLI warns on unsupported runtimes.
- Python 3.12-capable `uv` environment for Runtime ZIP assembly.

Set environment variables:

```bash
export AWS_REGION=us-east-1
```

`BEDROCK_MODEL_ID` is optional and controls the model used by the deployed triage agent. If you do not set it, `deploy.sh` and `setup-demo.sh` use the Anthropic Claude Opus 4.6 geo inference profile `us.anthropic.claude-opus-4-6-v1`. To override the default, set any foundation model ID or inference profile ID supported by the target Region:

```bash
export BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1
```

`BEDROCK_EVALUATOR_MODEL_ID` is optional and controls the AgentCore Evaluator's LLM-as-judge model. It defaults to the lower-cost judge model used in current AgentCore Evaluations examples:

```bash
export BEDROCK_EVALUATOR_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

If you use a named profile, set `AWS_PROFILE=<your-profile>` before running the scripts. Otherwise, the scripts use the default AWS credential chain.

If your CDK bootstrap assets bucket uses customer-managed SSE-KMS encryption, set the exact bootstrap KMS key ARN before deployment:

```bash
export CDK_BOOTSTRAP_KMS_KEY_ARN=arn:aws:kms:${AWS_REGION}:123456789012:key/<key-id>
```

The deploy script checks the `CDKToolkit` `FileAssetsBucket` encryption configuration after bootstrap. When that bucket uses `aws:kms` or `aws:kms:dsse`, deployment fails closed unless `CDK_BOOTSTRAP_KMS_KEY_ARN` is set. Default bootstrap buckets that do not use SSE-KMS do not require this variable.

### Bedrock model-access notes

For third-party models, Amazon Bedrock may need to enable the model for the account before the first successful invocation. Anthropic models also require a first-time-use form. If either the Runtime model or evaluator model is not enabled, live calls or AgentCore Evaluator creation can fail with `AccessDeniedException` mentioning `aws-marketplace:ViewSubscriptions` or `aws-marketplace:Subscribe`.

Recommended setup for a sample or production account:

1. Use an administrator or provisioning role to enable model access as a one-time account setup task.
2. Grant the deployed AgentCore Runtime only the Bedrock invoke permissions it needs.
3. Do not grant AWS Marketplace subscription permissions to the application runtime role just to make first invocation succeed.
4. Confirm the provisioning role can create an AgentCore Evaluator with the configured `BEDROCK_EVALUATOR_MODEL_ID`.
5. Rerun `./scripts/run-case.sh missing_ssi` and `./scripts/run-evaluation.sh` after model access is enabled if you need model-backed metrics.

`ENABLE_AGENTCORE_EVALUATOR` defaults to `true`. Set `ENABLE_AGENTCORE_EVALUATOR=false` only when the account is not yet authorized for evaluator creation and you need to deploy the rest of the sample while account setup is completed.

The browser-facing synthetic demo API is always protected by Amazon Cognito Hosted UI. The stack creates a Cognito user pool, user-pool client, Hosted UI domain, and API Gateway Cognito authorizer on every deployment. Public self-registration and the Cognito user-password auth flow are disabled for the demo user pool; create or invite demo users through Cognito before sharing the protected UI. After sign-in, the UI sends the returned ID token in the `Authorization` header for API calls, and the API validates the token audience against the generated demo app client. There is no environment variable, script option, or CDK context flag that removes browser-facing authentication.

The sample includes deterministic fallback stage logic so the synthetic demo remains testable when model calls are unavailable. Treat fallback-backed evaluation metrics as sample control-flow validation, not as claims about model quality.

## Dependency Reproducibility

Direct Python dependencies are pinned in `requirements.txt`, `requirements.runtime.txt`, and `requirements.streaming.txt`. Frontend direct dependencies are pinned in `frontend/package.json` and resolved by `frontend/package-lock.json`.

`scripts/deploy.sh` installs Python dependencies with `uv pip install -r ...` and builds the frontend with `npm ci`. `scripts/deploy-ui.sh` also uses `npm ci`. If dependency files change, run the full `./scripts/deploy.sh` path so Lambda and AgentCore Runtime assets are rebuilt from the pinned manifests.

## Required Commands

For a clean sample deployment, run the end-to-end setup command:

```bash
./scripts/setup-demo.sh
```

`setup-demo.sh` runs these phases in order: deploy infrastructure and UI, seed the synthetic data, attach the AgentCore Policy Engine, create or update Cedar policies, switch the Gateway to `ENFORCE`, and verify one allowed and one denied Gateway tool call.

The underlying phase scripts are kept intentionally small so you can rerun only the failed or changed phase. Running `deploy.sh` directly does not configure or verify AgentCore Policy, so it requires an explicit acknowledgement; use `setup-demo.sh` for the normal end-to-end path.

```bash
I_UNDERSTAND_DEPLOY_ONLY_WITHOUT_POLICY=1 ./scripts/deploy.sh
./scripts/seed-data.sh
./scripts/configure-policy.sh
./scripts/verify-policy.sh
```

Fast-path deploy for frontend-only changes (skips Python venv, runtime ZIP rebuilds, and CloudFormation; just rebuilds the Vite bundle, syncs the UI S3 bucket, and invalidates CloudFront — ~15–30 s vs ~3 min for the full deploy):

```bash
./scripts/deploy-ui.sh
```

Only use `deploy-ui.sh` when the changes are limited to `frontend/`. If any of `src/`, `infra/`, `app.py`, `cdk.json`, or `requirements*.txt` have changed, run `./scripts/deploy.sh` instead so CloudFormation picks up the new template and Lambda code.

Run the main demo case:

```bash
./scripts/run-case.sh missing_ssi
```

Run evaluation:

```bash
./scripts/run-evaluation.sh
```

Destroy:

```bash
./scripts/destroy.sh
```

Destroy a named stack or a stack in a non-default Region:

```bash
./scripts/destroy.sh agentic-post-trade-exception-triage-12 us-east-1
# or
./scripts/destroy.sh --stack-name agentic-post-trade-exception-triage-12 --region us-east-1
# or
STACK_NAME=agentic-post-trade-exception-triage-12 AWS_REGION=us-east-1 ./scripts/destroy.sh
```

## Expected Deploy Outputs

`setup-demo.sh` should print:

- Cloudscape UI URL.
- API Gateway URL.
- Step Functions state machine ARN.
- AgentCore Runtime ARN or ID.
- Agent framework: Strands Agents.
- AgentCore Gateway ARN or ID.
- CloudWatch dashboard URL.
- S3 bucket names for UI and evaluation artifacts.
- DynamoDB table name.

## Expected Run Outputs

`run-case.sh missing_ssi` should print:

- Step Functions execution ARN.
- Case ID.
- Final status.
- Recommendation JSON.
- Validation decision.
- Routing or escalation outcome.
- Cloudscape deep link for the case.
- Step Functions console link.
- CloudWatch dashboard link.

## Expected Evaluation Outputs

`run-evaluation.sh` should print:

- Evaluation run ID.
- Golden dataset location.
- Playbook accuracy.
- Evidence recall.
- Escalation correctness.
- Policy-denial correctness.
- Invalid output rate.
- Unauthorized tool attempt rate.
- Latency summary.
- Link to evaluation artifacts.

## Destroy Behavior

`destroy.sh` must remove:

- CDK stacks.
- Post-deploy AgentCore Policy Engine policies created by `configure-policy.sh`.
- AgentCore resources if not deleted by CDK.
- Seeded synthetic DynamoDB data.
- S3 objects created by the sample.
- CloudFront distribution and UI bucket.

The destroy script is intentionally explicit and safe. It prints the account and region before deleting resources, resolves the Gateway and Policy Engine IDs from CloudFormation outputs, detaches the post-deploy Policy Engine configuration from the Gateway, deletes any policies in that engine, waits until the engine is empty, and then invokes `cdk destroy`. This pre-cleanup is required because the Gateway attachment and Cedar policies are created after deployment and are not CloudFormation-managed resources; AgentCore rejects Policy Engine deletion while it is still attached to a Gateway or still contains policies.

## Response Streaming (feature flag)

This sample ships a **response-streaming capability** that gives the Cloudscape UI live stage-by-stage visibility into the AgentCore Runtime's work. It is controlled by a single CDK context flag and defaults to **on**.

### Flag surface

| Surface | Key | Values | Default |
|---|---|---|---|
| `scripts/deploy.sh` env var | `RESPONSE_STREAMING` | `true` / `false` (and common variants: `1`, `0`, `yes`, `no`, `on`, `off`) | `true` |
| CDK context (direct or via `cdk deploy -c ...`) | `responseStreaming` | `true` / `false` | `true` (set in `cdk.json`) |
| Runtime config for the UI | `window.__TRIAGE_CONFIG.responseStreaming` | boolean | deploy-time |

`scripts/deploy.sh` forwards `RESPONSE_STREAMING` to `cdk deploy` as `-c responseStreaming=<value>`. `cdk.json` pins the default so `cdk deploy` without the flag still ships streaming on.

### What ships when the flag is on (default)

- A new Lambda `StreamingInvokeFn` (Python 3.12 / ARM64, fronted by [AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter) as a managed layer) with its own IAM role `StreamingInvokeRole`.
- A new API Gateway route `GET /executions-stream?case_key=<key>` on the existing `TriageApi`, with integration `responseTransferMode = STREAM` (the Amazon API Gateway REST streaming feature announced in Nov 2025; see [Set up a Lambda proxy integration with payload response streaming](https://docs.aws.amazon.com/apigateway/latest/developerguide/response-transfer-mode-lambda.html)).
- The AgentCore Runtime's `/invocations` handler learns a second shape: when the caller sets `Accept: text/event-stream`, it returns an SSE stream with one frame per agent stage (summary, evidence, playbook, recommendation) plus a terminal `complete` frame carrying the full aggregated response. The original non-streaming shape is preserved byte-for-byte for Step Functions and the evaluator.
- Stack outputs `ResponseStreamingEnabled` and `StreamingEndpointUrl` are emitted so the UI and scripts can detect the flag without re-reading CDK context.
- `window.__TRIAGE_CONFIG.responseStreaming` is set to `true` in the injected `/config.js`, and `window.__TRIAGE_CONFIG.streamingEndpointUrl` is populated.

### What ships when the flag is off

- None of the above. The stack deploys exactly as the pre-feature baseline. Stack output `ResponseStreamingEnabled` is `false`; `StreamingEndpointUrl` is absent.
- UI reads the flag at load time and falls back to the existing poll-based progress animation. Everything keeps working; there just isn't a per-stage live sub-strip inside "Invoke AgentCore."

### ⚠️ Cost and correctness considerations (flag **on**)

1. **Double AgentCore invocation per demo run.** A single UI "Run triage" click invokes the AgentCore Runtime **twice**: once through `StreamingInvokeFn` for live UI feedback, and once through Step Functions for the authoritative audit record. Each invocation consumes Bedrock inference tokens. For the 8-case demo dataset this roughly doubles Bedrock spend per demo session.
2. **The two invocations may produce slightly different model text.** Both calls use the same model, deterministic gates, and same `case` payload, but Bedrock is not reproducible across calls. The *final recommendation* from both paths is guaranteed to pass the same deterministic validation (`validate_recommendation`, `policy_confidence_gate`) — the UI's live teaser text may just differ in wording from what eventually lands in the audit record. A unit test (`tests/test_streaming.py::test_streamed_final_matches_aggregated_for_missing_ssi`) keeps the two code paths from drifting on the in-process fallback, so streaming is only a transport change, not a logic change.
3. **`run-evaluation.sh` does NOT use the streaming path**, regardless of the flag. Evaluation metrics stay comparable to the Task 13 baseline (`eval-6bf410aab1`) because the evaluator continues to hit the non-streaming `/invocations` endpoint. `agent_invocation_mode` remains `DEPLOYED_RUNTIME`.
4. **Step Functions is unchanged.** The `InvokeAgentCoreFn` Lambda still calls `invoke_agent_runtime(accept="application/json")` and still records the authoritative recommendation. Audit records, correctness properties, and all other Step Functions behavior are unaffected by the flag.
5. **Flipping the flag requires a CloudFormation update.** You cannot toggle streaming at runtime. Redeploy via `I_UNDERSTAND_DEPLOY_ONLY_WITHOUT_POLICY=1 RESPONSE_STREAMING=false ./scripts/deploy.sh` (or `true`) to switch, or use `RESPONSE_STREAMING=false ./scripts/setup-demo.sh` for a full rerun. CloudFormation will create or destroy `StreamingInvokeFn`, `StreamingInvokeRole`, and the API Gateway `executions-stream` method. No other resources are affected.
6. **Maximum streaming duration is 15 minutes.** The streaming API Gateway integration extends the 29-second REST default to 15 minutes, which is substantially longer than any realistic AgentCore Runtime call. If a stream does exceed that window it's terminated server-side; the SFN path is unaffected.

### How to turn it off

```bash
# One-off for the current deploy
I_UNDERSTAND_DEPLOY_ONLY_WITHOUT_POLICY=1 RESPONSE_STREAMING=false ./scripts/deploy.sh

# Or permanent: set "responseStreaming": false in cdk.json
```

**Note**: flipping the flag requires the full deploy phase because it creates or destroys CloudFormation resources (`StreamingInvokeFn`, `StreamingInvokeRole`, the API Gateway method). Use `scripts/setup-demo.sh` for the normal end-to-end path, or `scripts/deploy.sh` for a targeted infrastructure rerun. The `scripts/deploy-ui.sh` fast-path is frontend-only and will not change the flag's deployed state.

## Post-Deploy Step: Configure AgentCore Policy

This sample creates the AgentCore Gateway and Policy Engine in CloudFormation, then creates Cedar policies in a post-deploy step. The two-phase flow is intentional because Cedar policies that scope to a specific Gateway require the Gateway ARN after the Gateway has been created. For normal setup, `scripts/setup-demo.sh` runs this step automatically. Skipping policy configuration or verification requires `I_UNDERSTAND_UNENFORCED_POLICY=1`; otherwise `setup-demo.sh` fails closed instead of leaving the Gateway outside verified `ENFORCE` mode.

Run:

```bash
./scripts/configure-policy.sh
./scripts/verify-policy.sh
```

`configure-policy.sh` performs the following:

- Reads the Gateway ARN, Gateway URL, and Policy Engine ARN from CloudFormation outputs.
- Generates Cedar files under `build/policies/`.
- Attaches the Policy Engine to the Gateway in `LOG_ONLY` while the policies are created and validated.
- Creates or updates one Policy Engine-scoped `permit` policy for the six read-only evidence tools.
- Creates or updates one Policy Engine-scoped `forbid` policy that denies `get_ssi_record` for restricted synthetic counterparties.
- Switches the Gateway Policy Engine to `ENFORCE`.

Policy names include a stable suffix derived from the Policy Engine ID. This keeps repeated setup runs idempotent while avoiding name collisions with policies left behind by earlier demo stacks in the same account and Region.

`verify-policy.sh` performs a live Gateway check:

- Confirms the Gateway Policy Engine mode is `ENFORCE`.
- Invokes an allowed read-only settlement status lookup through MCP.
- Attempts a restricted SSI lookup and expects AgentCore Policy to deny it before the Lambda target executes.

By default, policy creation uses `POLICY_VALIDATION_MODE=FAIL_ON_ANY_FINDINGS`. Use `POLICY_VALIDATION_MODE=IGNORE_ALL_FINDINGS` only for temporary service troubleshooting, not as the sample's normal path.

## Related documents

- [Pre-deployment checklist](PRE_DEPLOY_CHECKLIST.md) — run through this before `./scripts/setup-demo.sh`.
