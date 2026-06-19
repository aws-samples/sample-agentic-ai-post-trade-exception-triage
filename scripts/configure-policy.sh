#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
AWS_REGION="${AWS_REGION:-us-east-1}"
TOOL_SEPARATOR="${GATEWAY_TOOL_SEPARATOR:-___}"
VALIDATION_MODE="${POLICY_VALIDATION_MODE:-FAIL_ON_ANY_FINDINGS}"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "${PYTHON_BIN}" ] && [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Checking Python SDK support for AgentCore Policy Engine configuration"
if ! "${PYTHON_BIN}" - <<'PY'
try:
    import boto3
except ImportError:
    raise SystemExit(1)

client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
shape = client.meta.service_model.operation_model("UpdateGateway").input_shape
raise SystemExit(0 if "policyEngineConfiguration" in shape.members else 1)
PY
then
  echo "ERROR: the active Python environment does not expose AgentCore update_gateway(policyEngineConfiguration)." >&2
  echo "Run ./scripts/deploy.sh first, or install current Python dependencies with: uv pip install -r requirements.txt" >&2
  exit 1
fi

echo "==> Resolving stack outputs for ${STACK_NAME} in ${AWS_REGION}"
OUTPUTS="$(aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${AWS_REGION}" \
  --query 'Stacks[0].Outputs' \
  --output json)"

output_value() {
  python3 -c 'import json,sys; key=sys.argv[1]; outputs=json.load(sys.stdin); print(next(item["OutputValue"] for item in outputs if item["OutputKey"] == key))' "$1" <<<"${OUTPUTS}"
}

GATEWAY_ID="$(output_value AgentCoreGatewayId)"
GATEWAY_ARN="$(output_value AgentCoreGatewayArn)"
POLICY_ENGINE_ID="$(output_value AgentCorePolicyEngineId)"
POLICY_ENGINE_ARN="$(output_value AgentCorePolicyEngineArn)"
TARGET_NAME="${GATEWAY_TARGET_NAME:-$(output_value AgentCoreGatewayTargetName)}"
ACTION_PREFIX="${TARGET_NAME}${TOOL_SEPARATOR}"
POLICY_NAME_SUFFIX="$("${PYTHON_BIN}" - "${POLICY_ENGINE_ID}" <<'PY'
import hashlib
import sys

policy_engine_id = sys.argv[1]
print(hashlib.sha1(policy_engine_id.encode("utf-8")).hexdigest()[:10])
PY
)"
READ_ONLY_POLICY_NAME="${READ_ONLY_POLICY_NAME:-PostTradeReadOnlyAllow_${POLICY_NAME_SUFFIX}}"
RESTRICTED_SSI_POLICY_NAME="${RESTRICTED_SSI_POLICY_NAME:-PostTradeRestrictedSsiForbid_${POLICY_NAME_SUFFIX}}"

mkdir -p build/policies

ALLOW_POLICY_FILE="build/policies/post-trade-read-only-allow.cedar"
FORBID_POLICY_FILE="build/policies/post-trade-restricted-ssi-forbid.cedar"
GATEWAY_UPDATE_FILE="build/policies/update-gateway-policy-engine.json"

cat > "${ALLOW_POLICY_FILE}" <<CEDAR
permit(
  principal is AgentCore::IamEntity,
  action in [
    AgentCore::Action::"${ACTION_PREFIX}get_trade_details",
    AgentCore::Action::"${ACTION_PREFIX}get_settlement_status",
    AgentCore::Action::"${ACTION_PREFIX}get_allocation_status",
    AgentCore::Action::"${ACTION_PREFIX}get_ssi_record",
    AgentCore::Action::"${ACTION_PREFIX}search_prior_cases",
    AgentCore::Action::"${ACTION_PREFIX}get_playbook"
  ],
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
);
CEDAR

cat > "${FORBID_POLICY_FILE}" <<CEDAR
forbid(
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"${ACTION_PREFIX}get_ssi_record",
  resource == AgentCore::Gateway::"${GATEWAY_ARN}"
)
when {
  context.input.counterparty_id like "CP-SYN-RESTRICTED*"
};
CEDAR

wait_for_gateway() {
  local expected_mode="$1"
  for _ in $(seq 1 30); do
    local state status mode reasons
    state="$("${PYTHON_BIN}" - "${AWS_REGION}" "${GATEWAY_ID}" <<'PY'
import json
import sys

import boto3

region = sys.argv[1]
gateway_id = sys.argv[2]

client = boto3.client("bedrock-agentcore-control", region_name=region)
gateway = client.get_gateway(gatewayIdentifier=gateway_id)
print(json.dumps(gateway, default=str))
PY
)"
    status="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))' <<<"${state}")"
    mode="$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("policyEngineConfiguration") or {}).get("mode", ""))' <<<"${state}")"
    if [ "${status}" = "READY" ] && [ "${mode}" = "${expected_mode}" ]; then
      echo "    Gateway ${GATEWAY_ID}: READY (${expected_mode})"
      return 0
    fi
    if [[ "${status}" == *FAILED ]] || [ "${status}" = "UPDATE_UNSUCCESSFUL" ]; then
      reasons="$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("statusReasons", [])))' <<<"${state}")"
      echo "ERROR: gateway ${GATEWAY_ID} reached ${status}: ${reasons}" >&2
      exit 1
    fi
    sleep 10
  done
  echo "ERROR: gateway ${GATEWAY_ID} did not become READY in ${expected_mode} mode in time" >&2
  exit 1
}

update_gateway_policy_mode() {
  local mode="$1"
  local gateway_state
  gateway_state="$(aws bedrock-agentcore-control get-gateway \
    --region "${AWS_REGION}" \
    --gateway-identifier "${GATEWAY_ID}" \
    --output json)"
  GATEWAY_STATE="${gateway_state}" python3 - "${mode}" "${POLICY_ENGINE_ARN}" "${GATEWAY_UPDATE_FILE}" <<'PY'
import json
import os
import sys

mode = sys.argv[1]
policy_engine_arn = sys.argv[2]
output_path = sys.argv[3]
gateway = json.loads(os.environ["GATEWAY_STATE"])

payload = {
    "gatewayIdentifier": gateway["gatewayId"],
    "name": gateway["name"],
    "roleArn": gateway["roleArn"],
    "protocolType": gateway["protocolType"],
    "authorizerType": gateway["authorizerType"],
    "policyEngineConfiguration": {
        "arn": policy_engine_arn,
        "mode": mode,
    },
}
for optional_key in (
    "description",
    "protocolConfiguration",
    "authorizerConfiguration",
    "kmsKeyArn",
    "interceptorConfigurations",
    "exceptionLevel",
):
    value = gateway.get(optional_key)
    if value not in (None, "", [], {}):
        payload[optional_key] = value

with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2)
PY
  "${PYTHON_BIN}" - "${AWS_REGION}" "${GATEWAY_UPDATE_FILE}" <<'PY'
import json
import sys

import boto3

region = sys.argv[1]
payload_path = sys.argv[2]

with open(payload_path, encoding="utf-8") as handle:
    payload = json.load(handle)

client = boto3.client("bedrock-agentcore-control", region_name=region)
client.update_gateway(**payload)
PY
  wait_for_gateway "${mode}"
}

echo "==> Attaching Policy Engine to Gateway in LOG_ONLY for schema validation"
update_gateway_policy_mode "LOG_ONLY"

create_or_update_policy() {
  local name="$1"
  local description="$2"
  local policy_file="$3"
  local response action policy_id

  response="$("${PYTHON_BIN}" - "${AWS_REGION}" "${POLICY_ENGINE_ID}" "${name}" "${description}" "${policy_file}" "${VALIDATION_MODE}" <<'PY'
import json
import sys

import boto3

region, policy_engine_id, name, description, policy_file, validation_mode = sys.argv[1:]

with open(policy_file, encoding="utf-8") as handle:
    statement = handle.read()

definition = {"cedar": {"statement": statement}}
client = boto3.client("bedrock-agentcore-control", region_name=region)

policies = []
next_token = None
while True:
    request = {"policyEngineId": policy_engine_id, "maxResults": 100}
    if next_token:
        request["nextToken"] = next_token
    response = client.list_policies(**request)
    policies.extend(response.get("policies", []))
    next_token = response.get("nextToken")
    if not next_token:
        break

existing = next((policy for policy in policies if policy.get("name") == name), None)
if existing:
    policy_id = existing["policyId"]
    client.update_policy(
        policyEngineId=policy_engine_id,
        policyId=policy_id,
        description={"optionalValue": description},
        validationMode=validation_mode,
        definition=definition,
    )
    print(json.dumps({"action": "Updating", "policyId": policy_id}))
else:
    created = client.create_policy(
        policyEngineId=policy_engine_id,
        name=name,
        description=description,
        validationMode=validation_mode,
        definition=definition,
    )
    print(json.dumps({"action": "Creating", "policyId": created["policyId"]}))
PY
)"
  action="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])' <<<"${response}")"
  policy_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["policyId"])' <<<"${response}")"
  echo "==> ${action} policy ${name} (${policy_id})"
  wait_for_policy "${policy_id}"
}

wait_for_policy() {
  local policy_id="$1"
  for _ in $(seq 1 30); do
    local state status reasons
    state="$("${PYTHON_BIN}" - "${AWS_REGION}" "${POLICY_ENGINE_ID}" "${policy_id}" <<'PY'
import json
import sys

import boto3

region, policy_engine_id, policy_id = sys.argv[1:]
client = boto3.client("bedrock-agentcore-control", region_name=region)
policy = client.get_policy(policyEngineId=policy_engine_id, policyId=policy_id)
print(json.dumps(policy, default=str))
PY
)"
    status="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))' <<<"${state}")"
    if [ "${status}" = "ACTIVE" ]; then
      echo "    ${policy_id}: ACTIVE"
      return 0
    fi
    if [[ "${status}" == *FAILED ]]; then
      reasons="$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("statusReasons", [])))' <<<"${state}")"
      echo "ERROR: policy ${policy_id} reached ${status}: ${reasons}" >&2
      exit 1
    fi
    sleep 5
  done
  echo "ERROR: policy ${policy_id} did not become ACTIVE in time" >&2
  exit 1
}

create_or_update_policy \
  "${READ_ONLY_POLICY_NAME}" \
  "Permit IAM-authenticated Runtime callers to invoke the six read-only synthetic post-trade evidence tools." \
  "${ALLOW_POLICY_FILE}"

create_or_update_policy \
  "${RESTRICTED_SSI_POLICY_NAME}" \
  "Deny SSI lookup for restricted synthetic counterparties before the target Lambda executes." \
  "${FORBID_POLICY_FILE}"

echo "==> Switching Gateway Policy Engine to ENFORCE"
update_gateway_policy_mode "ENFORCE"

echo "Done. Gateway ${GATEWAY_ID} is configured with Policy Engine ${POLICY_ENGINE_ID} in ENFORCE mode."
echo "Generated Cedar files:"
echo "  ${ALLOW_POLICY_FILE}"
echo "  ${FORBID_POLICY_FILE}"
