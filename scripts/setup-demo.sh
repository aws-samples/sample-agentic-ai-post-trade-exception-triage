#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
export STACK_NAME
export AWS_REGION="${AWS_REGION:-us-east-1}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-opus-4-6-v1}"
export BEDROCK_EVALUATOR_MODEL_ID="${BEDROCK_EVALUATOR_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"

SKIP_DEPLOY="${SKIP_DEPLOY:-false}"
SKIP_SEED="${SKIP_SEED:-false}"
SKIP_POLICY="${SKIP_POLICY:-false}"
SKIP_VERIFY="${SKIP_VERIFY:-false}"

is_true() {
  case "$1" in
    true|1|yes|on|TRUE|True|YES|ON) return 0 ;;
    *) return 1 ;;
  esac
}

run_step() {
  local label="$1"
  shift
  echo
  echo "==> ${label}"
  "$@"
}

echo "Post-trade exception triage demo setup"
echo "Stack:   ${STACK_NAME}"
echo "Region:  ${AWS_REGION}"
echo "Profile: ${AWS_PROFILE:-<default credential chain>}"
echo "Runtime model:   ${BEDROCK_MODEL_ID}"
echo "Evaluator model: ${BEDROCK_EVALUATOR_MODEL_ID}"
echo "Demo auth:       Cognito Hosted UI enabled"

if { is_true "${SKIP_POLICY}" || is_true "${SKIP_VERIFY}"; } && [ "${I_UNDERSTAND_UNENFORCED_POLICY:-}" != "1" ]; then
  echo "ERROR: skipping policy configuration or verification can leave AgentCore Gateway outside ENFORCE mode." >&2
  echo "Re-run with I_UNDERSTAND_UNENFORCED_POLICY=1 only for controlled advanced reruns." >&2
  exit 2
fi

if ! is_true "${SKIP_DEPLOY}"; then
  run_step "Deploy infrastructure, Runtime, Gateway, UI, and workflow" env SETUP_DEMO_ORCHESTRATED=1 "${SCRIPT_DIR}/deploy.sh"
else
  echo
  echo "==> Skipping deploy because SKIP_DEPLOY=${SKIP_DEPLOY}"
fi

if ! is_true "${SKIP_SEED}"; then
  run_step "Load synthetic post-trade data" "${SCRIPT_DIR}/seed-data.sh"
else
  echo
  echo "==> Skipping seed because SKIP_SEED=${SKIP_SEED}"
fi

if ! is_true "${SKIP_POLICY}"; then
  run_step "Configure AgentCore Policy and switch Gateway to ENFORCE" "${SCRIPT_DIR}/configure-policy.sh"
else
  echo
  echo "==> Skipping policy configuration because SKIP_POLICY=${SKIP_POLICY}"
fi

if ! is_true "${SKIP_VERIFY}"; then
  run_step "Verify allowed and denied Gateway tool calls" "${SCRIPT_DIR}/verify-policy.sh"
else
  echo
  echo "==> Skipping policy verification because SKIP_VERIFY=${SKIP_VERIFY}"
fi

echo
echo "==> Resolving demo URLs"
OUTPUTS="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --query 'Stacks[0].Outputs' \
  --output json)"

output_value() {
  OUTPUTS_JSON="${OUTPUTS}" python3 - "$1" <<'PY'
import json
import os
import sys

key = sys.argv[1]
outputs = json.loads(os.environ["OUTPUTS_JSON"])
for item in outputs:
    if item.get("OutputKey") == key:
        print(item.get("OutputValue", ""))
        break
PY
}

cloudscape_url="$(output_value CloudscapeUiUrl)"
api_url="$(output_value ApiUrl)"
dashboard_name="$(output_value CloudWatchDashboardName)"
gateway_target_name="$(output_value AgentCoreGatewayTargetName)"
state_machine_arn="$(output_value StateMachineArn)"
demo_auth_domain="$(output_value DemoAuthHostedUiDomain)"

cat <<EOF

Demo setup complete.

Cloudscape UI: ${cloudscape_url}
API URL:        ${api_url}
Dashboard:      ${dashboard_name}
Gateway target: ${gateway_target_name}
State machine:  ${state_machine_arn}
Demo auth:      Cognito Hosted UI enabled
Hosted UI:      ${demo_auth_domain:-<not enabled>}

Next useful commands:
  ./scripts/run-case.sh missing_ssi
  ./scripts/run-evaluation.sh
  ./scripts/destroy.sh ${STACK_NAME} ${AWS_REGION}

Advanced reruns:
  SKIP_DEPLOY=true ./scripts/setup-demo.sh     # reseed + reconfigure/verify policy
  SKIP_SEED=true ./scripts/setup-demo.sh       # redeploy + policy only
  I_UNDERSTAND_UNENFORCED_POLICY=1 SKIP_POLICY=true ./scripts/setup-demo.sh
EOF
