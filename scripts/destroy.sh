#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./scripts/destroy.sh [STACK_NAME] [AWS_REGION]
  ./scripts/destroy.sh --stack-name STACK_NAME [--region AWS_REGION]

Environment variables are also supported:
  STACK_NAME=my-stack AWS_REGION=us-east-2 ./scripts/destroy.sh
EOF
}

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
AWS_REGION="${AWS_REGION:-us-east-1}"
positional_stack_seen=false
positional_region_seen=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --stack-name)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --stack-name requires a value." >&2
        usage >&2
        exit 2
      fi
      STACK_NAME="$2"
      shift 2
      ;;
    --stack-name=*)
      STACK_NAME="${1#--stack-name=}"
      shift
      ;;
    --region)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --region requires a value." >&2
        usage >&2
        exit 2
      fi
      AWS_REGION="$2"
      shift 2
      ;;
    --region=*)
      AWS_REGION="${1#--region=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while [ "$#" -gt 0 ]; do
        if [ "${positional_stack_seen}" = "false" ]; then
          STACK_NAME="$1"
          positional_stack_seen=true
        elif [ "${positional_region_seen}" = "false" ]; then
          AWS_REGION="$1"
          positional_region_seen=true
        else
          echo "ERROR: unexpected argument: $1" >&2
          usage >&2
          exit 2
        fi
        shift
      done
      ;;
    -*)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [ "${positional_stack_seen}" = "false" ]; then
        STACK_NAME="$1"
        positional_stack_seen=true
      elif [ "${positional_region_seen}" = "false" ]; then
        AWS_REGION="$1"
        positional_region_seen=true
      else
        echo "ERROR: unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      shift
      ;;
  esac
done

if [ -z "${STACK_NAME}" ]; then
  echo "ERROR: stack name cannot be empty." >&2
  usage >&2
  exit 2
fi

if [ -z "${AWS_REGION}" ]; then
  echo "ERROR: AWS region cannot be empty." >&2
  usage >&2
  exit 2
fi

export STACK_NAME
export AWS_REGION

CDK_CLI_VERSION="${CDK_CLI_VERSION:-2.1123.0}"
CDK_CMD=(npx --yes "aws-cdk@${CDK_CLI_VERSION}")
# Profile handling: respect AWS_PROFILE if set by the shell; otherwise let the
# default credential chain resolve. See scripts/deploy.sh for rationale.

account="$(aws sts get-caller-identity --query Account --output text)"
profile_label="${AWS_PROFILE:-default credential chain}"
echo "Destroying ${STACK_NAME} in account=${account} region=${AWS_REGION} profile=${profile_label}"
echo "CDK CLI: aws-cdk@${CDK_CLI_VERSION}"
echo "This removes CDK-managed AgentCore, Step Functions, Lambda, DynamoDB, S3, CloudFront, API, and dashboard resources."

if ! aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
  echo "Stack ${STACK_NAME} does not exist in ${AWS_REGION}. Nothing to destroy."
  exit 0
fi

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "${PYTHON_BIN}" ] && [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi
PYTHON_BIN="${PYTHON_BIN:-python3}"

CDK_PROFILE_FLAG=()
if [ -n "${AWS_PROFILE:-}" ]; then
  CDK_PROFILE_FLAG=(--profile "${AWS_PROFILE}")
fi

stack_output_value() {
  local outputs="$1"
  local key="$2"
  python3 -c 'import json,sys
outputs=json.load(sys.stdin) or []
key=sys.argv[1]
print(next((item.get("OutputValue", "") for item in outputs if item.get("OutputKey") == key), ""))' "${key}" <<<"${outputs}"
}

cleanup_agentcore_policy_attachment() {
  local outputs gateway_id policy_engine_arn

  echo "Checking for AgentCore Gateway Policy Engine attachment to remove before stack destroy..."
  if ! outputs="$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query 'Stacks[0].Outputs' \
    --output json 2>/dev/null)"; then
    echo "Stack ${STACK_NAME} is not present in CloudFormation, or outputs are unavailable. Skipping AgentCore Gateway policy attachment cleanup."
    return 0
  fi

  gateway_id="$(stack_output_value "${outputs}" "AgentCoreGatewayId")"
  policy_engine_arn="$(stack_output_value "${outputs}" "AgentCorePolicyEngineArn")"

  if [ -z "${gateway_id}" ] || [ -z "${policy_engine_arn}" ]; then
    echo "No AgentCoreGatewayId or AgentCorePolicyEngineArn output found. Skipping AgentCore Gateway policy attachment cleanup."
    return 0
  fi

  "${PYTHON_BIN}" - "${AWS_REGION}" "${gateway_id}" "${policy_engine_arn}" <<'PY'
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

region, gateway_id, policy_engine_arn = sys.argv[1:]
client = boto3.client("bedrock-agentcore-control", region_name=region)

required_operations = {"GetGateway", "UpdateGateway"}
available_operations = set(client.meta.service_model.operation_names)
missing_operations = sorted(required_operations - available_operations)
if missing_operations:
    print(
        "ERROR: the active Python SDK does not expose required AgentCore Gateway operations: "
        + ", ".join(missing_operations),
        file=sys.stderr,
    )
    print("Run ./scripts/deploy.sh first, or install current dependencies with: uv pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)


def is_not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {
        "ResourceNotFoundException",
        "ResourceNotFound",
        "NotFoundException",
    }


def get_gateway() -> dict | None:
    try:
        return client.get_gateway(gatewayIdentifier=gateway_id)
    except ClientError as error:
        if is_not_found(error):
            print(f"AgentCore Gateway {gateway_id} no longer exists. Skipping policy attachment cleanup.")
            return None
        raise


def wait_for_gateway_ready() -> dict | None:
    for _ in range(60):
        gateway = get_gateway()
        if gateway is None:
            return None
        status = gateway.get("status", "")
        if status == "READY":
            return gateway
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}:
            reasons = json.dumps(gateway.get("statusReasons", []))
            print(f"ERROR: AgentCore Gateway {gateway_id} reached {status}: {reasons}", file=sys.stderr)
            raise SystemExit(1)
        print(f"Waiting for AgentCore Gateway {gateway_id} to become READY before detaching Policy Engine; current status={status}")
        time.sleep(5)
    print(f"ERROR: AgentCore Gateway {gateway_id} did not become READY in time.", file=sys.stderr)
    raise SystemExit(1)


gateway = wait_for_gateway_ready()
if gateway is None:
    raise SystemExit(0)

configuration = gateway.get("policyEngineConfiguration") or {}
if not configuration:
    print(f"AgentCore Gateway {gateway_id} has no Policy Engine attachment.")
    raise SystemExit(0)

attached_arn = configuration.get("arn", "")
if attached_arn and attached_arn != policy_engine_arn:
    print(
        f"AgentCore Gateway {gateway_id} is attached to a different Policy Engine ({attached_arn}); detaching it before stack destroy."
    )
else:
    print(f"Detaching Policy Engine from AgentCore Gateway {gateway_id}.")

payload = {
    "gatewayIdentifier": gateway["gatewayId"],
    "name": gateway["name"],
    "roleArn": gateway["roleArn"],
    "protocolType": gateway["protocolType"],
    "authorizerType": gateway["authorizerType"],
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

client.update_gateway(**payload)

for _ in range(60):
    gateway = get_gateway()
    if gateway is None:
        raise SystemExit(0)
    configuration = gateway.get("policyEngineConfiguration") or {}
    if not configuration:
        print(f"AgentCore Gateway {gateway_id} no longer has a Policy Engine attachment.")
        raise SystemExit(0)
    status = gateway.get("status", "")
    if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}:
        reasons = json.dumps(gateway.get("statusReasons", []))
        print(f"ERROR: AgentCore Gateway {gateway_id} reached {status}: {reasons}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Waiting for Policy Engine detach from AgentCore Gateway {gateway_id}; current status={status}")
    time.sleep(5)

print(f"ERROR: AgentCore Gateway {gateway_id} still has a Policy Engine attachment after waiting.", file=sys.stderr)
raise SystemExit(1)
PY
}

cleanup_agentcore_policies() {
  local outputs policy_engine_id

  echo "Checking for AgentCore Policy Engine policies to remove before stack destroy..."
  if ! outputs="$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query 'Stacks[0].Outputs' \
    --output json 2>/dev/null)"; then
    echo "Stack ${STACK_NAME} is not present in CloudFormation, or outputs are unavailable. Skipping AgentCore policy cleanup."
    return 0
  fi

  policy_engine_id="$(stack_output_value "${outputs}" "AgentCorePolicyEngineId")"

  if [ -z "${policy_engine_id}" ]; then
    echo "No AgentCorePolicyEngineId output found. Skipping AgentCore policy cleanup."
    return 0
  fi

  "${PYTHON_BIN}" - "${AWS_REGION}" "${policy_engine_id}" <<'PY'
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

region, policy_engine_id = sys.argv[1:]
client = boto3.client("bedrock-agentcore-control", region_name=region)

required_operations = {"ListPolicies", "DeletePolicy"}
available_operations = set(client.meta.service_model.operation_names)
missing_operations = sorted(required_operations - available_operations)
if missing_operations:
    print(
        "ERROR: the active Python SDK does not expose required AgentCore Policy Engine operations: "
        + ", ".join(missing_operations),
        file=sys.stderr,
    )
    print("Run ./scripts/deploy.sh first, or install current dependencies with: uv pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1)


def is_not_found(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in {
        "ResourceNotFoundException",
        "ResourceNotFound",
        "NotFoundException",
    }


def list_policies() -> list[dict]:
    policies = []
    next_token = None
    while True:
        request = {"policyEngineId": policy_engine_id, "maxResults": 100}
        if next_token:
            request["nextToken"] = next_token
        try:
            response = client.list_policies(**request)
        except ClientError as error:
            if is_not_found(error):
                print(f"AgentCore Policy Engine {policy_engine_id} no longer exists. Skipping policy cleanup.")
                return []
            raise
        policies.extend(response.get("policies", []))
        next_token = response.get("nextToken")
        if not next_token:
            return policies


policies = list_policies()
if not policies:
    print(f"AgentCore Policy Engine {policy_engine_id} has no policies.")
    raise SystemExit(0)

print(
    "Deleting AgentCore Policy Engine policies: "
    + json.dumps([{"policyId": p.get("policyId"), "name": p.get("name"), "status": p.get("status")} for p in policies])
)
for policy in policies:
    policy_id = policy["policyId"]
    try:
        client.delete_policy(policyEngineId=policy_engine_id, policyId=policy_id)
    except ClientError as error:
        if is_not_found(error):
            continue
        raise

for _ in range(60):
    remaining = list_policies()
    if not remaining:
        print(f"AgentCore Policy Engine {policy_engine_id} is empty.")
        raise SystemExit(0)
    print("Waiting for policy deletion: " + json.dumps([p.get("policyId") for p in remaining]))
    time.sleep(5)

print(f"ERROR: AgentCore Policy Engine {policy_engine_id} still contains policies after waiting.", file=sys.stderr)
raise SystemExit(1)
PY
}

cleanup_agentcore_policy_attachment
cleanup_agentcore_policies

# The app defines exactly one stack, using the stackName context above. Destroy
# --all avoids the CDK CLI's positional stack parser, which can emit noisy
# unknown stack-option warnings in some CLI builds.
"${CDK_CMD[@]}" destroy --all --force "${CDK_PROFILE_FLAG[@]}" -c stackName="${STACK_NAME}" -c account="${account}" -c region="${AWS_REGION}" -c enableEvaluator="${ENABLE_AGENTCORE_EVALUATOR:-true}"
