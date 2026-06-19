#!/usr/bin/env bash
set -euo pipefail

CASE_KEY="${1:-missing_ssi}"
STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
# Profile handling: respect AWS_PROFILE if set by the shell; otherwise let the
# default credential chain resolve. See scripts/deploy.sh for rationale.
export AWS_REGION="${AWS_REGION:-us-east-1}"

outputs="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --query 'Stacks[0].Outputs' --output json)"
STATE_MACHINE_ARN="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="StateMachineArn"))' <<<"${outputs}")"
UI_URL="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="CloudscapeUiUrl"))' <<<"${outputs}")"
DASHBOARD="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="CloudWatchDashboardName"))' <<<"${outputs}")"

payload="$(python3 - <<PY
import json
print(json.dumps({"exception": {"case_key": "${CASE_KEY}"}}))
PY
)"
execution_arn="$(aws stepfunctions start-execution --state-machine-arn "${STATE_MACHINE_ARN}" --name "${CASE_KEY}-$(date +%s)" --input "${payload}" --query executionArn --output text)"
echo "Step Functions execution ARN: ${execution_arn}"

while true; do
  status="$(aws stepfunctions describe-execution --execution-arn "${execution_arn}" --query status --output text)"
  case "${status}" in
    RUNNING) sleep 3 ;;
    *) break ;;
  esac
done

execution_json="$(aws stepfunctions describe-execution --execution-arn "${execution_arn}" --output json)"
status="$(EXECUTION_JSON="${execution_json}" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["EXECUTION_JSON"]).get("status", "UNKNOWN"))
PY
)"

if [[ "${status}" != "SUCCEEDED" ]]; then
  EXECUTION_JSON="${execution_json}" python3 - <<'PY' >&2
import json
import os

data = json.loads(os.environ["EXECUTION_JSON"])
print(f"Final status: {data.get('status', 'UNKNOWN')}")
print(f"Error: {data.get('error', 'n/a')}")
cause = data.get("cause", "n/a")
if len(cause) > 4000:
    cause = cause[:4000] + "... [truncated]"
print("Cause:")
print(cause)
PY
  exit 1
fi

result="$(EXECUTION_JSON="${execution_json}" python3 - <<'PY'
import json
import os

print(json.loads(os.environ["EXECUTION_JSON"]).get("output", "{}"))
PY
)"
RESULT_JSON="${result}" python3 - <<'PY'
import json,sys
import os
data=json.loads(os.environ["RESULT_JSON"])
print(f"Case ID: {data['case']['exception_id']}")
print(f"Final status: {data.get('final_status')}")
print(f"Validation decision: {data.get('validation',{}).get('decision')}")
print(f"Routing decision: {data.get('routing',{}).get('routing_decision')}")
print("Recommendation JSON:")
print(json.dumps(data.get("agent",{}).get("recommendation",{}), indent=2))
PY
account="$(aws sts get-caller-identity --query Account --output text)"
encoded="${execution_arn//:/%3A}"
echo "Cloudscape deep link: ${UI_URL}"
echo "Step Functions console: https://${AWS_REGION}.console.aws.amazon.com/states/home?region=${AWS_REGION}#/executions/details/${encoded}"
echo "CloudWatch dashboard: https://${AWS_REGION}.console.aws.amazon.com/cloudwatch/home?region=${AWS_REGION}#dashboards:name=${DASHBOARD}"
echo "Account: ${account}"
