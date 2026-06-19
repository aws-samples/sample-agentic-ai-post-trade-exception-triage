# Agentic Post-Trade Exception Triage

This repository contains a deployable AWS sample for agent-assisted post-trade exception triage with Amazon Bedrock AgentCore. It is intentionally synthetic: it does not replace a clearing, settlement, reconciliation, or case-management platform, and it must not be used with real financial, customer, counterparty, account, trade, settlement, or operational data.

The sample demonstrates a governed pattern:

- A deterministic Step Functions control layer decides whether a case is eligible for agent assistance.
- Amazon Bedrock AgentCore Runtime hosts a Strands Agents workflow with summary, evidence, playbook, and recommendation stages.
- Runtime evidence access goes through Amazon Bedrock AgentCore Gateway over MCP; the deployed Runtime does not read the synthetic data store directly.
- AgentCore Policy authorizes Gateway tool calls with Cedar policies and moves to `ENFORCE` after validation.
- Recommendations remain advisory and require human approval.

## Important

- This is a sample application, not a production reference implementation.
- The browser demo UI/API is protected by Amazon Cognito Hosted UI by default. Public self-registration is disabled, so create or invite demo users through Cognito before sharing the protected UI.
- Gateway tools are read-only and case-scoped: every tool call carries the active `exception_id`, and the tool Lambda rejects cross-case counterparty, account, root-cause, and playbook requests.
- API Gateway throttling, a regional AWS WAF web ACL with AWS managed rules and a per-IP rate rule, CloudFront-only CORS, Lambda reserved concurrency, and a CloudWatch execution-volume alarm bound casual misuse. The browser-facing demo API is always deployed behind Cognito Hosted UI.
- Deploying this sample creates billable AWS resources, including AgentCore, Lambda, Step Functions, DynamoDB, S3, CloudFront, API Gateway, CloudWatch, and Amazon Bedrock model invocations.
- Run `./scripts/destroy.sh` when you are finished.

## Quick Start

Prerequisites: AWS CLI v2 with AgentCore Policy support, `uv`, Node.js 22 or 24 with npm, CDK bootstrap permissions, Amazon Bedrock model access in the target Region, and permission to create an AgentCore Evaluator with the configured evaluator model.

```bash
export AWS_REGION=us-east-1

./scripts/setup-demo.sh
./scripts/run-case.sh missing_ssi
./scripts/run-evaluation.sh
```

If you use a named AWS profile, set `AWS_PROFILE` before running the scripts. Otherwise, the scripts use the default AWS credential chain. The `setup-demo.sh` command deploys the stack, loads synthetic data, configures AgentCore Policy in `ENFORCE`, verifies one allowed and one denied Gateway tool call, and then prints the Cloudscape UI URL. With the default Cognito protection, create or invite a demo user in the generated user pool before signing in to the UI.

To deploy multiple independent copies in the same account and Region, use a different stack name for each copy:

```bash
STACK_NAME=AgenticPostTradeExceptionTriageStackDev ./scripts/setup-demo.sh
STACK_NAME=AgenticPostTradeExceptionTriageStackTest ./scripts/setup-demo.sh
```

The stack avoids fixed physical names for globally or account-scoped resources. AgentCore resources that require a name use stack-derived generated names, and scripts resolve the generated values from CloudFormation outputs.

Demo authentication:

```bash
./scripts/setup-demo.sh
```

This always adds an Amazon Cognito user pool and Hosted UI in front of the browser-facing API. The demo user pool does not allow public self-registration; create or invite demo users through Cognito before sharing the protected UI. There is no deployment flag that removes browser-facing authentication.

### Bedrock Model Access

`BEDROCK_MODEL_ID` controls the model used by the deployed triage agent. It is optional. If you do not set it, the scripts use the Anthropic Claude Opus 4.6 geo inference profile `us.anthropic.claude-opus-4-6-v1`. You can override it with any foundation model ID or inference profile ID supported by the target Region:

```bash
BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-6-v1 ./scripts/setup-demo.sh
```

`BEDROCK_EVALUATOR_MODEL_ID` controls the AgentCore Evaluator's LLM-as-judge model. It defaults to `us.anthropic.claude-haiku-4-5-20251001-v1:0`, matching the current AgentCore Evaluations examples for a lower-cost judge model. Override it when your account or Region requires another AgentCore Evaluator-supported model:

```bash
BEDROCK_EVALUATOR_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 ./scripts/setup-demo.sh
```

For Anthropic and other third-party models, the target account may need a one-time Amazon Bedrock model-access setup, AWS Marketplace permissions, and an Anthropic first-time-use form before live model calls succeed.

The sample keeps deterministic fallback stage logic so local tests and synthetic evaluation remain reproducible when model calls are disabled or unavailable. For blog or benchmark claims that describe model-backed behavior, enable the model in the target account and rerun:

```bash
./scripts/run-case.sh missing_ssi
./scripts/run-evaluation.sh
```

The phase scripts remain available for targeted reruns and debugging. `setup-demo.sh` is the normal path because it deploys, seeds, configures AgentCore Policy in `ENFORCE`, and verifies policy enforcement. Running `deploy.sh` directly deploys infrastructure only, so it requires an explicit acknowledgement:

```bash
I_UNDERSTAND_DEPLOY_ONLY_WITHOUT_POLICY=1 ./scripts/deploy.sh
./scripts/seed-data.sh
./scripts/configure-policy.sh
./scripts/verify-policy.sh
```

The AgentCore Evaluator resource is enabled by default. Some accounts can invoke a model from Bedrock Runtime but still fail evaluator creation if `BEDROCK_EVALUATOR_MODEL_ID` is not supported by AgentCore Evaluations in the target Region, is not authorized for the account, or AWS Marketplace subscription actions are blocked. If your account is not yet approved, you can deploy the rest of the sample with the evaluator disabled:

```bash
ENABLE_AGENTCORE_EVALUATOR=false ./scripts/setup-demo.sh
```

Destroy the sample when finished:

```bash
./scripts/destroy.sh
```

For a non-default stack name or Region:

```bash
./scripts/destroy.sh AgenticPostTradeExceptionTriageStackDev us-east-2
# or
STACK_NAME=AgenticPostTradeExceptionTriageStackDev AWS_REGION=us-east-2 ./scripts/destroy.sh
```

The destroy script also detaches the post-deploy AgentCore Policy Engine from the Gateway and removes the post-deploy policies before invoking CDK, so the stack can be deleted cleanly after running the full demo setup.

## What It Deploys

- CDK Python infrastructure in `infra/`.
- DynamoDB, S3, KMS, Step Functions Standard Workflow, Lambda task handlers, and CloudWatch dashboard.
- Amazon Bedrock AgentCore Runtime, Gateway, Policy Engine, and Evaluator via CloudFormation L1 resources. The Evaluator can be disabled only as an account-readiness fallback.
- Lambda-backed read-only Gateway tools over synthetic datasets in `data/`.
- A Cloudscape demo UI in `frontend/`.

Start with [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for deployment details and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the architecture.

## Evaluation

Run the deterministic golden-case evaluator after deployment:

```bash
./scripts/run-evaluation.sh
```

The evaluator records:

- Golden case count and invocation mode.
- Playbook accuracy, evidence recall, escalation correctness, and policy-denial correctness.
- Invalid output rate and unauthorized tool attempt rate.
- p50 and p95 triage latency.

## Security Notes

This is a sample, not a production reference implementation. The demo UI/API is protected by Cognito Hosted UI by default, but that demo protection is not a production tenant authorization model. Before adapting it to a production account, review [docs/SECURITY.md](docs/SECURITY.md), add customer identity and network controls, narrow the documented wildcard IAM permissions, and replace synthetic stores with governed systems-of-record integrations.

## Repository Hygiene

Generated artifacts are intentionally excluded from source control, including `.venv/`, `frontend/node_modules/`, `frontend/dist/`, `cdk.out/`, `build/`, `evaluation-output/`, `.hypothesis/`, `.kiro/`, and `cdk-outputs.json`.

Before publishing or opening a pull request, run:

```bash
./scripts/check-publication.sh
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidance.

## License

This sample is licensed under the MIT-0 License. See [LICENSE](LICENSE).
