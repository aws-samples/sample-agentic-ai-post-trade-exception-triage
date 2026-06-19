"""Centralized cdk-nag suppressions for AgenticPostTradeTriageStack.

Each suppression references docs/SECURITY.md as the rationale for accepting
sample-only findings. Production adaptations must narrow or remove these
suppressions.
"""

from __future__ import annotations

from aws_cdk import Stack
from cdk_nag import NagSuppressions


_SECURITY_DOC = "docs/SECURITY.md"


def apply_nag_suppressions(stack: Stack) -> None:
    """Apply cdk-nag suppressions to the AgenticPostTradeTriageStack.

    Only sample-appropriate findings listed in the security guide are suppressed.
    Findings the stack already remediates (SF1, SF2, S10) are NOT suppressed.
    """
    # We use resource-level add_resource_suppressions_by_path with the stack's
    # node.path as the base so the suppressions apply to paths inside this stack.
    base = stack.node.path

    # --- Stack-wide IAM findings tied to AWSLambdaBasicExecutionRole on Lambda roles ---
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-IAM4",
                "reason": (
                    "AWSLambdaBasicExecutionRole is the minimum required managed policy for "
                    "CloudWatch Logs and is documented as acceptable for aws-samples. "
                    f"See {_SECURITY_DOC}."
                ),
                "applies_to": [
                    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                ],
            },
            {
                "id": "AwsSolutions-IAM5",
                "reason": (
                    "Wildcard resources accepted in the sample: (a) selected Bedrock "
                    "foundation-model resources use a model-id-derived ARN pattern because "
                    "inference profiles can route to associated foundation models; (b) S3 "
                    "and DynamoDB grants expand to per-table/per-bucket action "
                    "allowlists that cdk-nag flags as wildcards; (c) CloudWatch, X-Ray, and "
                    "AgentCore statements that require wildcard resources or service "
                    "creation-order trade-offs are documented. The AgentCore Runtime invoke "
                    "permission is scoped to the generated Runtime ARN and its DEFAULT "
                    "runtime endpoint ARN. Narrow remaining "
                    "trade-offs before production use where the target environment permits it "
                    f"per {_SECURITY_DOC}."
                ),
                "applies_to": [
                    "Resource::*",
                    "Action::s3:Abort*",
                    "Action::s3:DeleteObject*",
                    "Action::s3:GetBucket*",
                    "Action::s3:GetObject*",
                    "Action::s3:List*",
                    "Action::s3:PutObject*",
                    "Action::kms:GenerateDataKey*",
                    "Action::kms:ReEncrypt*",
                    {
                        "regex": "/^Resource::<.+\\.Arn>.*\\*$/g",
                    },
                    {
                        "regex": "/^Resource::.*\\*$/g",
                    },
                ],
            },
        ],
    )

    # --- API Gateway (demo UI API): sample path requires production hardening ---
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-APIG1",
                "reason": (
                    f"Sample UI API; enable method access logging before production use. "
                    f"See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-APIG2",
                "reason": (
                    f"Sample UI API; Lambda validates supported routes and payloads. "
                    f"Add API Gateway request models before production use. See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-APIG3",
                "reason": (
                    f"Sample UI API is protected by Cognito, a regional AWS WAF web ACL with "
                    f"AWS managed rules and a per-IP rate rule, API Gateway throttling, "
                    f"CloudFront-only CORS, Lambda reserved concurrency, and execution-volume "
                    f"alarms. Add organization-specific WAF rules before production use. "
                    f"See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-APIG4",
                "reason": (
                    f"Sample UI API is always protected by Amazon Cognito Hosted UI. Add "
                    f"enterprise identity and tenant authorization before production use. See "
                    f"{_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-APIG6",
                "reason": (
                    f"Sample UI API; enable method-level CloudWatch logging before production use. "
                    f"See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-COG4",
                "reason": (
                    f"Sample UI API always uses a Cognito user-pool authorizer. Add "
                    f"enterprise identity and tenant authorization before production use. See "
                    f"{_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-COG2",
                "reason": (
                    f"Demo auth protects the browser-facing synthetic demo from casual "
                    f"unauthenticated use. MFA is intentionally not required for this lightweight "
                    f"sample user pool. Add enterprise MFA before production "
                    f"use. See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-COG8",
                "reason": (
                    f"Demo auth avoids paid Cognito advanced security features to keep the "
                    f"AWS Samples deployment lightweight. Use your organization's user pool tier and "
                    f"advanced security controls before production use. See {_SECURITY_DOC}."
                ),
            },
        ],
    )

    # --- CloudFront (demo UI distribution): no edge WAF, no geo restrictions, no access logging ---
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-CFR1",
                "reason": (
                    f"Sample UI distribution; add geo restrictions if required by your "
                    f"control baseline. See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-CFR2",
                "reason": (
                    f"Sample UI distribution; the browser-facing API has a regional WAF. "
                    f"Attach a CloudFront WAF before production use if edge-layer controls "
                    f"are required. See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-CFR3",
                "reason": (
                    f"Sample UI distribution; enable CloudFront access logging before "
                    f"production use. See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-CFR4",
                "reason": (
                    "Demo UI distribution uses the default CloudFront certificate which "
                    "enforces TLSv1. Add a custom domain and stricter TLS policy before "
                    f"production use. See {_SECURITY_DOC}."
                ),
            },
            {
                "id": "AwsSolutions-CFR7",
                "reason": (
                    "Demo UI distribution uses S3BucketOrigin.with_origin_access_control; "
                    "cdk-nag may still flag legacy OAI checks. OAC is the current best "
                    "practice."
                ),
            },
        ],
    )

    # --- Lambda runtime currency ---
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-L1",
                "reason": (
                    "PYTHON_3_12 is the current stable Python runtime at time of writing. "
                    f"Managed log-retention (one week) is documented per {_SECURITY_DOC}."
                ),
            },
        ],
    )

    # --- CloudWatch Logs: demo Lambda log groups ---
    NagSuppressions.add_stack_suppressions(
        stack,
        [
            {
                "id": "AwsSolutions-CW1",
                "reason": (
                    "Demo Lambda log groups use AWS-managed CloudWatch Logs encryption. "
                    "Customer-managed KMS encryption of log groups adds cost and operational "
                    f"overhead; enable if required by your control baseline. See {_SECURITY_DOC}."
                ),
            },
        ],
    )

    # --- S3 server access logging on demo buckets ---
    NagSuppressions.add_resource_suppressions_by_path(
        stack,
        f"{base}/ArtifactBucket/Resource",
        [
            {
                "id": "AwsSolutions-S1",
                "reason": (
                    "Demo artifact bucket with auto-delete and KMS encryption. Server access "
                    "logging deferred; bucket content is synthetic and non-sensitive per "
                    f"{_SECURITY_DOC}."
                ),
            },
        ],
    )
    NagSuppressions.add_resource_suppressions_by_path(
        stack,
        f"{base}/UiBucket/Resource",
        [
            {
                "id": "AwsSolutions-S1",
                "reason": (
                    "Demo UI bucket with auto-delete, SSE-S3 encryption, and private "
                    "CloudFront OAC access. Server access logging deferred; bucket "
                    "content is the public SPA build per "
                    f"{_SECURITY_DOC}."
                ),
            },
        ],
    )
