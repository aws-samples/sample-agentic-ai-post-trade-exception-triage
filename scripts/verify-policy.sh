#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export GATEWAY_TOOL_SEPARATOR="${GATEWAY_TOOL_SEPARATOR:-___}"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "${PYTHON_BIN}" ] && [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Resolving stack outputs for ${STACK_NAME} in ${AWS_REGION}"
OUTPUTS="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --query 'Stacks[0].Outputs' \
  --output json)"

output_value() {
  python3 -c 'import json,sys; key=sys.argv[1]; outputs=json.load(sys.stdin); print(next(item["OutputValue"] for item in outputs if item["OutputKey"] == key))' "$1" <<<"${OUTPUTS}"
}

export AGENTCORE_GATEWAY_URL="$(output_value AgentCoreGatewayUrl)"
export GATEWAY_TARGET_NAME="${GATEWAY_TARGET_NAME:-$(output_value AgentCoreGatewayTargetName)}"
GATEWAY_ID="$(output_value AgentCoreGatewayId)"

echo "==> Verifying Gateway Policy Engine is in ENFORCE mode"
MODE="$("${PYTHON_BIN}" - "${AWS_REGION}" "${GATEWAY_ID}" <<'PY'
import sys

import boto3

region = sys.argv[1]
gateway_id = sys.argv[2]

client = boto3.client("bedrock-agentcore-control", region_name=region)
gateway = client.get_gateway(gatewayIdentifier=gateway_id)
print((gateway.get("policyEngineConfiguration") or {}).get("mode", ""))
PY
)"
if [ "${MODE}" != "ENFORCE" ]; then
  echo "ERROR: Gateway ${GATEWAY_ID} is in ${MODE}, expected ENFORCE. Run ./scripts/configure-policy.sh first." >&2
  exit 1
fi

echo "==> Invoking allowed read-only tool and denied restricted SSI lookup"
PYTHONPATH=. "${PYTHON_BIN}" - <<'PY'
from src.agentcore_runtime.tools.gateway_client import AgentCoreGatewayClient, GatewayClientError

client = AgentCoreGatewayClient()

allowed = client.get_settlement_status("EXC-SYN-10042")
if not isinstance(allowed, dict) or allowed.get("source_id") != "settlement_status:EXC-SYN-10042":
    raise SystemExit(f"Allowed tool call returned an unexpected payload: {allowed!r}")

try:
    denied = client.get_ssi_record("EXC-SYN-10047", "CP-SYN-RESTRICTED-01", "ACCT-SYN-8817")
except GatewayClientError as exc:
    print(f"Policy deny verified: {exc}")
else:
    raise SystemExit(f"Restricted SSI lookup was not denied by AgentCore Policy: {denied!r}")

print("Policy verification passed.")
PY
