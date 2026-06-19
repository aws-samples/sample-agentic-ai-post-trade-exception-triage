#!/usr/bin/env bash
# Fast-path deploy for frontend-only changes. Rebuilds the Vite bundle, syncs
# the UI S3 bucket, and invalidates the CloudFront distribution. Takes ~15-30
# seconds versus ~3 minutes for a full ./scripts/deploy.sh.
#
# Use this script ONLY when:
#   - Python source under src/ has not changed.
#   - CDK infra (infra/, cdk.json, app.py) has not changed.
#   - Python deps (requirements*.txt) have not changed.
#
# If any of those have changed, run ./scripts/deploy.sh instead — CloudFormation
# needs to see the updated template and/or Lambda asset hashes.
#
# Preserves the runtime-injected /config.js that the CDK stack writes by
# rebuilding it from CloudFormation outputs after each frontend sync.

set -euo pipefail

STACK_NAME="${STACK_NAME:-AgenticPostTradeExceptionTriageStack}"
export AWS_REGION="${AWS_REGION:-us-east-1}"

echo "==> Pre-flight"

if ! CALLER="$(aws sts get-caller-identity --output json 2>/dev/null)"; then
  echo "ERROR: aws sts get-caller-identity failed. Set AWS_PROFILE or export AWS_ACCESS_KEY_ID/SECRET/SESSION." >&2
  exit 1
fi
ACCOUNT_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["Account"])' <<<"${CALLER}")"
echo "Account: ${ACCOUNT_ID}"
echo "Region:  ${AWS_REGION}"
echo "Stack:   ${STACK_NAME}"

# Guard: warn (not fail) if the user has uncommitted changes outside frontend/,
# which is almost always a sign they should run the full deploy.sh instead.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  NON_FRONTEND_CHANGES="$(git -P status --short | awk '{print $2}' | grep -Ev '^(frontend/|\.gitignore$|docs/|\.kiro/)' || true)"
  if [ -n "${NON_FRONTEND_CHANGES}" ]; then
    echo ""
    echo "WARNING: Uncommitted changes outside frontend/ detected:"
    echo "${NON_FRONTEND_CHANGES}" | sed 's/^/  /'
    echo "  If these include infra, Python source, or dependency files, run"
    echo "  ./scripts/deploy.sh instead — scripts/deploy-ui.sh only ships the UI."
    echo ""
  fi
fi

echo "==> Resolving stack outputs"
OUTPUTS="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${AWS_REGION}" --query 'Stacks[0].Outputs' --output json)"
UI_BUCKET="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="UiBucketName"))' <<<"${OUTPUTS}")"
UI_URL="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="CloudscapeUiUrl"))' <<<"${OUTPUTS}")"
API_URL="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next(x["OutputValue"] for x in o if x["OutputKey"]=="ApiUrl"))' <<<"${OUTPUTS}")"
RESPONSE_STREAMING="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next((x["OutputValue"] for x in o if x["OutputKey"]=="ResponseStreamingEnabled"), "false"))' <<<"${OUTPUTS}")"
STREAMING_ENDPOINT="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next((x["OutputValue"] for x in o if x["OutputKey"]=="StreamingEndpointUrl"), ""))' <<<"${OUTPUTS}")"
DASHBOARD_NAME="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next((x["OutputValue"] for x in o if x["OutputKey"]=="CloudWatchDashboardName"), ""))' <<<"${OUTPUTS}")"
COGNITO_CLIENT_ID="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next((x["OutputValue"] for x in o if x["OutputKey"]=="DemoAuthUserPoolClientId"), ""))' <<<"${OUTPUTS}")"
COGNITO_HOSTED_UI_DOMAIN="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next((x["OutputValue"] for x in o if x["OutputKey"]=="DemoAuthHostedUiDomain"), ""))' <<<"${OUTPUTS}")"
COGNITO_USER_POOL_ID="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(next((x["OutputValue"] for x in o if x["OutputKey"]=="DemoAuthUserPoolId"), ""))' <<<"${OUTPUTS}")"
if [ -z "${COGNITO_CLIENT_ID}" ] || [ -z "${COGNITO_HOSTED_UI_DOMAIN}" ] || [ -z "${COGNITO_USER_POOL_ID}" ]; then
  echo "ERROR: Cognito Hosted UI outputs are missing. Re-run ./scripts/deploy.sh for this stack before using deploy-ui.sh." >&2
  exit 1
fi
echo "UiBucket: ${UI_BUCKET}"
echo "UiUrl:    ${UI_URL}"

# The stack writes CloudscapeUiUrl as https://<distribution_domain>; look up
# the distribution ID from the domain name. aws cloudfront list-distributions
# is cheap and doesn't require knowing the resource logical id.
DIST_DOMAIN="${UI_URL#https://}"
DIST_DOMAIN="${DIST_DOMAIN%/}"
DIST_ID="$(aws cloudfront list-distributions --query "DistributionList.Items[?DomainName=='${DIST_DOMAIN}'].Id | [0]" --output text)"
if [ -z "${DIST_ID}" ] || [ "${DIST_ID}" = "None" ]; then
  echo "ERROR: could not resolve CloudFront distribution ID for domain ${DIST_DOMAIN}" >&2
  exit 1
fi
echo "Distribution: ${DIST_ID}"

echo "==> Building frontend"
npm --prefix frontend ci >/dev/null 2>&1 || {
  echo "ERROR: npm ci failed" >&2
  exit 1
}
npm --prefix frontend run build

echo "==> Syncing frontend/dist to s3://${UI_BUCKET}/"
# Intentionally NOT using --delete. The first version of this script did, with
# --exclude "config.js" to try to protect the CDK-injected runtime config.
# That does not work: --exclude applies to source filtering, but --delete
# walks the destination looking for orphans and happily deletes config.js
# because it is not in the local frontend/dist/ tree. Dropping --delete
# leaves a few kilobytes of stale hashed assets in the bucket on each run,
# which is harmless (new index.html references a new hash) and a far better
# failure mode than breaking the UI every time.
aws s3 sync frontend/dist/ "s3://${UI_BUCKET}/" \
  --region "${AWS_REGION}"

echo "==> Uploading runtime config to s3://${UI_BUCKET}/config.js"
CONFIG_TMP="$(mktemp)"
trap 'rm -f "${CONFIG_TMP}"' EXIT
API_URL="${API_URL}" RESPONSE_STREAMING="${RESPONSE_STREAMING}" STREAMING_ENDPOINT="${STREAMING_ENDPOINT}" AWS_REGION="${AWS_REGION}" DASHBOARD_NAME="${DASHBOARD_NAME}" COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID}" COGNITO_HOSTED_UI_DOMAIN="${COGNITO_HOSTED_UI_DOMAIN}" COGNITO_USER_POOL_ID="${COGNITO_USER_POOL_ID}" UI_URL="${UI_URL}" python3 - <<'PY' > "${CONFIG_TMP}"
import json
import os

payload = {
    "apiUrl": os.environ["API_URL"],
    "responseStreaming": os.environ.get("RESPONSE_STREAMING", "false").lower() == "true",
    "streamingEndpointUrl": os.environ.get("STREAMING_ENDPOINT") or None,
    "awsRegion": os.environ["AWS_REGION"],
    "dashboardName": os.environ.get("DASHBOARD_NAME") or None,
    "auth": {
        "enabled": True,
        "userPoolId": os.environ.get("COGNITO_USER_POOL_ID") or None,
        "userPoolClientId": os.environ.get("COGNITO_CLIENT_ID") or None,
        "hostedUiDomain": os.environ.get("COGNITO_HOSTED_UI_DOMAIN") or None,
        "redirectUri": os.environ["UI_URL"],
        "logoutUri": os.environ["UI_URL"],
    },
}
print("window.__TRIAGE_CONFIG = " + json.dumps(payload, separators=(",", ":")) + ";")
PY
aws s3 cp "${CONFIG_TMP}" "s3://${UI_BUCKET}/config.js" \
  --region "${AWS_REGION}" \
  --content-type "application/javascript; charset=utf-8" \
  --cache-control "no-store, max-age=0"

echo "==> Invalidating CloudFront /*"
INVALIDATION_ID="$(aws cloudfront create-invalidation --distribution-id "${DIST_ID}" --paths '/*' --query 'Invalidation.Id' --output text)"
echo "Invalidation ID: ${INVALIDATION_ID}"
echo ""
echo "Done. CloudFront invalidation takes ~1-3 minutes to propagate."
echo "UI: ${UI_URL}"
echo ""
echo "Tip: hard-refresh the browser (Cmd-Shift-R) to skip the browser cache while"
echo "     CloudFront finishes invalidating."
