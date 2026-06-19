#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
# Profile handling: respect AWS_PROFILE if set by the shell; otherwise let the
# default credential chain resolve. See scripts/deploy.sh for rationale.
export AWS_REGION="${AWS_REGION:-us-east-1}"

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi

outputs="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --query 'Stacks[0].Outputs' --output json)"
export TABLE_NAME="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="DynamoTableName"))' <<<"${outputs}")"
export ARTIFACT_BUCKET="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="ArtifactBucketName"))' <<<"${outputs}")"

python3 scripts/seed_data.py
