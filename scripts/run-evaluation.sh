#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
# Profile handling: respect AWS_PROFILE if set by the shell; otherwise let the
# default credential chain resolve. See scripts/deploy.sh for rationale.
export AWS_REGION="${AWS_REGION:-us-east-1}"

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

DEPLOYED=0
if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" >/tmp/triage-stack.json 2>/dev/null; then
  outputs="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --query 'Stacks[0].Outputs' --output json)"
  export TABLE_NAME="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="DynamoTableName"))' <<<"${outputs}")"
  export ARTIFACT_BUCKET="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="ArtifactBucketName"))' <<<"${outputs}")"
  export AGENT_RUNTIME_ARN="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="AgentCoreRuntimeArn"))' <<<"${outputs}")"
  DEPLOYED=1
fi

# Local (no stack): keep deterministic fallback on. Deployed: let the Strands model call run.
if [ "${DEPLOYED}" -eq 0 ]; then
  export DISABLE_STRANDS_MODEL_CALL="${DISABLE_STRANDS_MODEL_CALL:-1}"
else
  unset DISABLE_STRANDS_MODEL_CALL
fi

mkdir -p evaluation-output
python3 -m src.evaluation.runner | tee evaluation-output/latest-evaluation.json
