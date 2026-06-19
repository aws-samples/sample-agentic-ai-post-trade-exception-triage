#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}"
export BEDROCK_EVALUATOR_MODEL_ID="${BEDROCK_EVALUATOR_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
# Pin the CDK CLI instead of using @latest. This keeps corporate-laptop runs
# reproducible and avoids CLI parser drift. Override only when deliberately
# testing a newer CLI: CDK_CLI_VERSION=<version> ./scripts/setup-demo.sh.
CDK_CLI_VERSION="${CDK_CLI_VERSION:-2.1123.0}"
CDK_CMD=(npx --yes "aws-cdk@${CDK_CLI_VERSION}")

# Feature flag: response streaming. Default true. Set RESPONSE_STREAMING=false
# to deploy without the streaming Lambda / streaming API Gateway route / UI
# streaming panel. When true, the UI shows live per-stage output but a single
# "Run triage" click invokes AgentCore twice — once through the streaming
# Lambda, once through Step Functions. See docs/DEPLOYMENT.md for the full
# trade-off write-up.
export RESPONSE_STREAMING="${RESPONSE_STREAMING:-true}"
case "${RESPONSE_STREAMING}" in
  true|1|yes|on|TRUE|True) RESPONSE_STREAMING=true ;;
  *) RESPONSE_STREAMING=false ;;
esac

# AgentCore Evaluator is enabled by default because the blog sample includes
# an LLM-as-judge evaluation asset. The target account must have Bedrock model
# and AWS Marketplace authorization for the selected evaluator model. Set
# ENABLE_AGENTCORE_EVALUATOR=false only for accounts that are not yet approved.
export ENABLE_AGENTCORE_EVALUATOR="${ENABLE_AGENTCORE_EVALUATOR:-true}"
case "${ENABLE_AGENTCORE_EVALUATOR}" in
  true|1|yes|on|TRUE|True) ENABLE_AGENTCORE_EVALUATOR=true ;;
  *) ENABLE_AGENTCORE_EVALUATOR=false ;;
esac

if [ "${SETUP_DEMO_ORCHESTRATED:-0}" != "1" ] && [ "${I_UNDERSTAND_DEPLOY_ONLY_WITHOUT_POLICY:-}" != "1" ]; then
  echo "ERROR: deploy.sh deploys infrastructure only and does not configure/verify AgentCore Policy ENFORCE mode." >&2
  echo "Use ./scripts/setup-demo.sh for the normal end-to-end path, or re-run deploy.sh with I_UNDERSTAND_DEPLOY_ONLY_WITHOUT_POLICY=1." >&2
  exit 2
fi

# Profile handling: if AWS_PROFILE is already set in the shell (including to empty),
# respect it and let the default credential chain resolve (env vars, SSO, instance
# profile, default profile). Users on corporate laptops often prefer to use
# 'default' or env-injected STS credentials.
if [ -z "${AWS_PROFILE:-}" ]; then
  # No profile set — don't pick one. Let the default chain do its thing.
  # Previous versions forced a named profile, which broke users whose
  # active creds were in 'default' or in env vars.
  :
fi

# Build the --profile flag for cdk only if AWS_PROFILE is non-empty.
CDK_PROFILE_FLAG=()
if [ -n "${AWS_PROFILE:-}" ]; then
  CDK_PROFILE_FLAG=(--profile "${AWS_PROFILE}")
fi

echo "==> Pre-flight checks"

# 1. Caller identity (fatal if missing)
if ! CALLER="$(aws sts get-caller-identity --output json 2>/dev/null)"; then
  echo "ERROR: aws sts get-caller-identity failed. Either set AWS_PROFILE to a profile with valid credentials, or ensure 'aws sts get-caller-identity' works with whatever credentials are currently resolved by the default chain." >&2
  exit 1
fi
ACCOUNT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["Account"])' <<<"${CALLER}")"
CALLER_ARN="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["Arn"])' <<<"${CALLER}")"
echo "Account: ${ACCOUNT_ID}"
echo "Caller:  ${CALLER_ARN}"
echo "Region:  ${AWS_REGION}"
echo "Profile: ${AWS_PROFILE:-<default credential chain>}"
echo "Response streaming: ${RESPONSE_STREAMING}"
echo "AgentCore Evaluator: ${ENABLE_AGENTCORE_EVALUATOR}"
echo "Demo auth: Cognito Hosted UI enabled"
echo "Runtime model: ${BEDROCK_MODEL_ID}"
echo "Evaluator model: ${BEDROCK_EVALUATOR_MODEL_ID}"
echo "CDK CLI: aws-cdk@${CDK_CLI_VERSION}"

# 2. Bedrock model identifier visibility (warning only; access and SCPs can
# still block invocation after a valid identifier is visible).
check_bedrock_identifier() {
  local label="$1"
  local identifier="$2"

  if aws bedrock get-inference-profile \
       --region "${AWS_REGION}" \
       --inference-profile-identifier "${identifier}" >/dev/null 2>&1; then
    echo "Bedrock ${label}: inference profile visible (${identifier})."
    return 0
  fi

  if aws bedrock get-foundation-model \
       --region "${AWS_REGION}" \
       --model-identifier "${identifier}" >/dev/null 2>&1; then
    echo "Bedrock ${label}: foundation model visible (${identifier})."
    return 0
  fi

  echo "WARNING: Bedrock ${label} model identifier is not visible in ${AWS_REGION}: ${identifier}. Confirm model availability, model access, Marketplace setup, and SCP/IAM permissions before relying on live model calls."
}

check_bedrock_identifier "runtime" "${BEDROCK_MODEL_ID}"
if [ "${ENABLE_AGENTCORE_EVALUATOR}" = "true" ]; then
  check_bedrock_identifier "evaluator" "${BEDROCK_EVALUATOR_MODEL_ID}"
fi

# 3. uv (required for ARM64 wheel bundling)
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed. AgentCore Runtime direct code deployment requires 'uv pip install --python-platform aarch64-manylinux2014'. Install with: 'pip install uv' or 'brew install uv'." >&2
  exit 1
fi
echo "uv: $(uv --version)"

echo "==> Pre-flight OK. Preparing AgentCore Runtime ZIP and deploying ${STACK_NAME} in ${AWS_REGION}"

# Build the venv with uv pinning Python 3.12 so pip is new enough to resolve
# modern wheels (strands-agents>=1.0 won't install on pip 21 / Python 3.9).
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install --upgrade pip
uv pip install -r requirements.txt

if [ -d frontend ]; then
  npm --prefix frontend ci
  npm --prefix frontend run build
fi

# --- Build the AgentCore Runtime direct code ZIP root BEFORE synth/bootstrap/deploy ---
# AgentCore Runtime requires Linux/ARM64 wheels. We stage them under build/runtime/
# next to the repo's src/ and data/ so CDK's s3_assets.Asset can zip the whole dir.
# This must happen before any `cdk` command because bootstrap itself invokes synth
# and synth requires the asset path to exist.
echo "==> Assembling build/runtime/ for AgentCore Runtime direct code deployment"
rm -rf build/runtime
mkdir -p build/runtime
uv pip install \
  --python-version 3.12 \
  --python-platform aarch64-manylinux_2_28 \
  --only-binary=:all: \
  --target build/runtime \
  -r requirements.runtime.txt
cp -R src build/runtime/
cp -R data build/runtime/
# Strip pycache / pyc that may have been copied in; AgentCore rejects non-arm64 bytecode.
find build/runtime -type d -name __pycache__ -prune -exec rm -rf {} +
find build/runtime -type f -name '*.pyc' -delete

# --- Build the streaming invoke Lambda ZIP (feature: responseStreaming) ---
# Only built when the flag is on, otherwise the CDK stack skips the resource
# entirely. Smaller than the AgentCore Runtime ZIP: just fastapi/uvicorn/boto3
# plus our own src/common and src/streaming_invoke/ modules. LWA layer is
# attached by the CDK stack; we just ship the web app.
if [ "${RESPONSE_STREAMING}" = "true" ]; then
  echo "==> Assembling build/streaming_invoke/ for the streaming Lambda (responseStreaming=true)"
  rm -rf build/streaming_invoke
  mkdir -p build/streaming_invoke
  uv pip install \
    --python-version 3.12 \
    --python-platform aarch64-manylinux_2_28 \
    --only-binary=:all: \
    --target build/streaming_invoke \
    -r requirements.streaming.txt
  # Copy only the modules the streaming Lambda imports — avoid dragging in
  # the AgentCore Runtime agent code (not needed, adds weight).
  mkdir -p build/streaming_invoke/src
  cp src/__init__.py build/streaming_invoke/src/
  cp -R src/common build/streaming_invoke/src/
  cp -R src/streaming_invoke build/streaming_invoke/src/
  cp -R data build/streaming_invoke/
  find build/streaming_invoke -type d -name __pycache__ -prune -exec rm -rf {} +
  find build/streaming_invoke -type f -name '*.pyc' -delete
  chmod 755 build/streaming_invoke/src/streaming_invoke/run.sh
else
  echo "==> responseStreaming=false — skipping streaming Lambda bundle"
  rm -rf build/streaming_invoke
fi

# CDK bootstrap (auto-remediate) — runs AFTER the asset is on disk so synth succeeds.
if ! aws cloudformation describe-stacks --stack-name CDKToolkit --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "==> CDK bootstrap not detected for ${ACCOUNT_ID}/${AWS_REGION}. Running 'cdk bootstrap'..."
  "${CDK_CMD[@]}" bootstrap "aws://${ACCOUNT_ID}/${AWS_REGION}" "${CDK_PROFILE_FLAG[@]}"
else
  echo "CDK bootstrap: already done."
fi

# If the CDK bootstrap assets bucket uses SSE-KMS, AgentCore Runtime needs the
# exact bootstrap key ARN to read its code ZIP. Do not fall back to wildcard KMS
# resources; fail early with an explicit prerequisite instead.
BOOTSTRAP_BUCKET="$(
  aws cloudformation describe-stack-resources \
    --stack-name CDKToolkit \
    --logical-resource-id FileAssetsBucket \
    --region "${AWS_REGION}" \
    --query 'StackResources[0].PhysicalResourceId' \
    --output text 2>/dev/null || true
)"
if [ -n "${BOOTSTRAP_BUCKET}" ] && [ "${BOOTSTRAP_BUCKET}" != "None" ]; then
  BOOTSTRAP_SSE_ALGORITHM="$(
    aws s3api get-bucket-encryption \
      --bucket "${BOOTSTRAP_BUCKET}" \
      --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
      --output text 2>/dev/null || true
  )"
  if [ "${BOOTSTRAP_SSE_ALGORITHM}" = "aws:kms" ] || [ "${BOOTSTRAP_SSE_ALGORITHM}" = "aws:kms:dsse" ]; then
    if [ -z "${CDK_BOOTSTRAP_KMS_KEY_ARN:-}" ]; then
      echo "ERROR: CDK bootstrap bucket ${BOOTSTRAP_BUCKET} uses ${BOOTSTRAP_SSE_ALGORITHM} encryption." >&2
      echo "Set CDK_BOOTSTRAP_KMS_KEY_ARN to the exact bootstrap KMS key ARN before deploying." >&2
      echo "Example: export CDK_BOOTSTRAP_KMS_KEY_ARN=arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/<key-id>" >&2
      exit 2
    fi
    echo "CDK bootstrap KMS key: ${CDK_BOOTSTRAP_KMS_KEY_ARN}"
  fi
fi

# The app defines exactly one stack, using the stackName context above. Deploy
# --all avoids the CDK CLI's positional stack parser, which can emit noisy
# unknown stack-option warnings in some CLI builds.
"${CDK_CMD[@]}" deploy --all --require-approval never "${CDK_PROFILE_FLAG[@]}" -c stackName="${STACK_NAME}" -c account="${ACCOUNT_ID}" -c region="${AWS_REGION}" -c responseStreaming="${RESPONSE_STREAMING}" -c enableEvaluator="${ENABLE_AGENTCORE_EVALUATOR}" --outputs-file cdk-outputs.json

python3 - <<PY
import json, os
out=json.load(open("cdk-outputs.json"))
stack=next(iter(out.values()))
print("\nDeployment outputs")
for key in ["CloudscapeUiUrl","StateMachineArn","AgentCoreRuntimeArn","AgentCoreGatewayId","AgentCoreGatewayArn","AgentCoreGatewayUrl","AgentCoreGatewayTargetName","AgentCorePolicyEngineId","AgentCorePolicyEngineArn","AgentCoreEvaluatorEnabled","AgentCoreEvaluatorArn","AgentCoreEvaluatorModelId","DemoAuthEnabled","DemoAuthUserPoolId","DemoAuthUserPoolClientId","DemoAuthHostedUiDomain","CloudWatchDashboardName","ExecutionVolumeAlarmName","ArtifactBucketName","DynamoTableName","ApiUrl","ResponseStreamingEnabled","StreamingEndpointUrl"]:
    if key in stack:
        print(f"{key}: {stack.get(key)}")
print("Agent framework: Strands Agents")
print("Packaging: AgentCore Runtime direct code (ZIP, ARM64 wheels)")
print(f"Response streaming: ${RESPONSE_STREAMING}")
print(f"AgentCore Evaluator: ${ENABLE_AGENTCORE_EVALUATOR}")
print("Demo auth: Cognito Hosted UI enabled")
print(f"Runtime model: ${BEDROCK_MODEL_ID}")
print(f"Evaluator model: ${BEDROCK_EVALUATOR_MODEL_ID}")
print("Policy: deploy.sh only deploys infrastructure. Run ./scripts/configure-policy.sh and ./scripts/verify-policy.sh, or use ./scripts/setup-demo.sh for the full path.")
print("Demo UI: open CloudscapeUiUrl after setup completes and sign in through the hosted UI. Synthetic data only.")
PY
