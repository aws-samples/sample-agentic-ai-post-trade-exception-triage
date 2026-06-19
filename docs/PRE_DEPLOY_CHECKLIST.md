# Pre-Deployment Checklist

Run through this list before `./scripts/setup-demo.sh`. Items marked **required** are fatal if missing; items marked **recommended** produce warnings only but should be verified.

## AWS account prerequisites

- [ ] **Required** — `aws sts get-caller-identity` returns a valid caller for the target account. If you use a named profile, set `AWS_PROFILE=<your-profile>`. Scripts default to `us-east-1` unless you set `AWS_REGION`.
- [ ] **Required** — Amazon Bedrock model access granted for the configured Runtime model `BEDROCK_MODEL_ID`. The default is the Anthropic geo inference profile `us.anthropic.claude-opus-4-6-v1`. For Anthropic models, complete the first-time-use form and ensure an administrator or provisioning role can perform the required AWS Marketplace model-access setup.
- [ ] **Required** — AgentCore Evaluator can be created with the configured judge model `BEDROCK_EVALUATOR_MODEL_ID`. The default is `us.anthropic.claude-haiku-4-5-20251001-v1:0`. The sample enables the Evaluator by default; if the target account is not yet authorized for evaluator creation, deploy temporarily with `ENABLE_AGENTCORE_EVALUATOR=false`.
- [ ] **Required** — Amazon Bedrock AgentCore is available in the target Region for the target account. AgentCore availability is still rolling out; check the service console or the "what's new" feed if the stack fails at an AgentCore resource.
- [ ] **Required** — CDK bootstrapped for the `<account>/<target-region>` target. `scripts/deploy.sh` runs `cdk bootstrap` automatically when the `CDKToolkit` stack is missing.
- [ ] **Recommended** — Credentials are valid for long enough to cover the deploy (20–40 minutes including CloudFront propagation).

## Local tooling

- [ ] **Required** — Python 3.12 available (the venv is pinned to 3.12 via `uv venv --python 3.12`; 3.10/3.11/3.13 would also work but 3.12 matches the deployed Lambda and AgentCore Runtime). `mise`, `pyenv`, Homebrew, or `uv python install 3.12` all work.
- [ ] **Required** — Node.js 22 or 24 for CDK and Vite build. Avoid odd-numbered non-LTS Node releases such as Node 23 because the CDK CLI warns on unsupported runtimes.
- [ ] **Required** — `uv` installed (`pip install uv` or `brew install uv`). Used to (a) pin the venv to Python 3.12, and (b) cross-install ARM64 Linux wheels for the AgentCore Runtime ZIP via `--python-platform aarch64-manylinux_2_28`. No Docker or container runtime needed.
- [ ] **Recommended** — `npx --yes aws-cdk@2.1123.0 --version` succeeds without proxy errors. Override the scripts with `CDK_CLI_VERSION=<version>` only when intentionally testing another CDK CLI.

## Feature flags

- [ ] **Response streaming (`RESPONSE_STREAMING`)** — default `true`. When `true`, the deploy provisions a streaming Lambda + LWA layer + API Gateway STREAM method so the UI can show live stage-by-stage output from the AgentCore Runtime. Trade-off: a single UI "Run triage" click invokes AgentCore Runtime twice (streaming endpoint + Step Functions), roughly doubling Bedrock spend for demo runs. Set `RESPONSE_STREAMING=false` to deploy the pre-feature baseline. See [`DEPLOYMENT.md` "Response Streaming (feature flag)"](DEPLOYMENT.md#response-streaming-feature-flag).
- [ ] **Demo auth** — mandatory. The stack adds Amazon Cognito Hosted UI and an API Gateway Cognito authorizer for the browser-facing synthetic demo API on every deployment. Public self-registration is disabled; create or invite demo users through Cognito before sharing the protected UI.
- [ ] **AgentCore Evaluator (`ENABLE_AGENTCORE_EVALUATOR`)** — default `true`. Keep it enabled for the blog sample. Set `ENABLE_AGENTCORE_EVALUATOR=false` only while account/model authorization for `BEDROCK_EVALUATOR_MODEL_ID` is being completed.

## Repo health

- [ ] `pip install -r requirements.txt` succeeds.
- [ ] `DISABLE_STRANDS_MODEL_CALL=1 python3 -m pytest -q` passes.
- [ ] `npm --prefix frontend install && npm --prefix frontend run build` succeeds.
- [ ] `CDK_DOCKER=echo npx --yes aws-cdk@2.1123.0 synth -c account=<account> -c region=<target-region> --quiet` exits 0 with no unsuppressed cdk-nag error-level findings.

## After deploy

- [ ] `./scripts/setup-demo.sh` completes deployment, synthetic data load, policy configuration, and policy verification.
- [ ] AgentCore Policy verification completes with Gateway mode `ENFORCE`. If you use `SKIP_POLICY` or `SKIP_VERIFY`, acknowledge the risk with `I_UNDERSTAND_UNENFORCED_POLICY=1` only for controlled reruns.
- [ ] `./scripts/run-case.sh missing_ssi` reaches `SUCCEEDED` in Step Functions.
- [ ] CloudWatch Logs show at least one line from the AgentCore Runtime log group.
- [ ] `./scripts/run-evaluation.sh` prints metrics meeting the documented thresholds.
- [ ] If publishing model-backed metrics, CloudWatch Runtime logs do not contain `Strands model call failed, using deterministic stage output`.

## UI-only iterations

After the initial deploy, UI-only changes (anything scoped to `frontend/`) can ship via the fast-path script instead of re-running the full deploy:

```bash
./scripts/deploy-ui.sh
```

Takes ~15–30 seconds vs ~3 minutes for `./scripts/deploy.sh`. See [`DEPLOYMENT.md` "Required Commands"](DEPLOYMENT.md#required-commands) for when to use each.
