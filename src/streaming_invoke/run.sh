#!/bin/sh
# Startup script for the streaming Lambda, invoked by AWS Lambda Web Adapter
# (AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap). The adapter waits for the web
# app to pass its readiness probe on AWS_LWA_PORT before forwarding any
# Lambda invocations.
#
# We use uvicorn with --http h11 (no HTTP/2 in Lambda) and a single worker
# because each Lambda container serves exactly one concurrent invocation.

exec python3 -m uvicorn src.streaming_invoke.app:app \
  --host 0.0.0.0 \
  --port "${AWS_LWA_PORT:-8080}" \
  --workers 1 \
  --log-level info \
  --no-access-log
