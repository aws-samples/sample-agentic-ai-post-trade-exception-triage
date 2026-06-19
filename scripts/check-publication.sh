#!/usr/bin/env bash
set -euo pipefail

bad_paths=(
  ".DS_Store"
  ".hypothesis"
  ".kiro"
  ".venv"
  "build"
  "cdk.out"
  "cdk-outputs.json"
  "evaluation-output"
  "frontend/dist"
  "frontend/node_modules"
)

found=0
for path in "${bad_paths[@]}"; do
  if [ -e "${path}" ]; then
    echo "Remove generated or local-only path before publishing: ${path}" >&2
    found=1
  fi
done

if [ "${found}" -ne 0 ]; then
  exit 1
fi

required_files=(
  "LICENSE"
  "CONTRIBUTING.md"
  "CODE_OF_CONDUCT.md"
  "SECURITY.md"
  "README.md"
)

for path in "${required_files[@]}"; do
  if [ ! -f "${path}" ]; then
    echo "Missing required repository file: ${path}" >&2
    exit 1
  fi
done

secret_matches="$(rg --hidden -n -S \
  "(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|aws_secret_access_key|aws_session_token|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|https://[a-z0-9]+\\.execute-api\\.)" \
  -g '!frontend/node_modules/**' \
  -g '!cdk.out/**' \
  -g '!build/**' \
  -g '!.venv/**' \
  -g '!package-lock.json' \
  -g '!scripts/check-publication.sh' \
  . || true)"

# Mock documentation accounts plus the public AWS Lambda Web Adapter layer publisher.
allowed_account_ids="000000000000|111111111111|111122223333|123456789012|444455556666|753240598075"

account_matches="$(rg --hidden -n -S "\\b[0-9]{12}\\b" \
  -g '!frontend/node_modules/**' \
  -g '!cdk.out/**' \
  -g '!build/**' \
  -g '!.venv/**' \
  -g '!package-lock.json' \
  -g '!scripts/check-publication.sh' \
  . | rg -v "\\b(${allowed_account_ids})\\b" || true)"

if [ -n "${secret_matches}" ] || [ -n "${account_matches}" ]; then
  printf "%s\n%s\n" "${secret_matches}" "${account_matches}" >&2
  echo "Potential secret, live endpoint, ARN, or account ID found. Review output above." >&2
  exit 1
fi

echo "Publication hygiene check passed."
