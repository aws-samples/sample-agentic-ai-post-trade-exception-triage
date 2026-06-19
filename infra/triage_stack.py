from __future__ import annotations

import os
import re
from pathlib import Path

from aws_cdk import (
    Aws,
    CfnOutput,
    CfnResource,
    Duration,
    Fn,
    Names,
    RemovalPolicy,
    Stack,
    aws_apigateway as apigw,
    aws_bedrock as bedrock,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cloudwatch as cloudwatch,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_s3_deployment as s3deploy,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct

from infra.nag_suppressions import apply_nag_suppressions


class AgenticPostTradeTriageStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        response_streaming: bool = True,
        enable_evaluator: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Feature flag: when True, create the streaming Lambda + LWA layer +
        # API Gateway STREAM integration for the live UI stage-by-stage
        # experience. When False, the stack deploys exactly as before the
        # feature landed: no new Lambda, no new API route, UI falls back to
        # the existing poll-based progress animation.
        self._response_streaming_enabled = response_streaming
        self._evaluator_enabled = enable_evaluator
        bedrock_model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
        evaluator_model_id = os.environ.get(
            "BEDROCK_EVALUATOR_MODEL_ID",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        )
        api_stage_name = self._generated_resource_name("ApiStage", "stage", max_length=64, separator="-")
        # Several AgentCore and auth resources require caller-assigned physical names.
        # Names.unique_id() is deterministic for a construct path and can collide with
        # orphaned resources left behind by a failed stack create. Derive those names
        # from the CloudFormation stack GUID instead; it is stable for stack updates
        # and unique for each new stack create attempt.
        stack_guid = Fn.select(2, Fn.split("/", Aws.STACK_ID))
        stack_guid_compact = Fn.join("", Fn.split("-", stack_guid))
        policy_engine_name = Fn.join("_", ["PostTradePE", stack_guid_compact])
        gateway_name = Fn.join("-", ["post-trade-gw", stack_guid_compact])
        gateway_target_name = Fn.join("-", ["post-trade-tools", stack_guid_compact])
        runtime_name = Fn.join("_", ["PostTradeRT", stack_guid_compact])
        evaluator_name = Fn.join("_", ["PostTradeEval", stack_guid_compact]) if enable_evaluator else ""
        guardrail_name = Fn.join("-", ["PostTradeGR", stack_guid_compact])
        ui_oac_name = Fn.join("-", ["post-trade-ui-oac", stack_guid])

        data_key = kms.Key(
            self,
            "DataKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        agentcore_key = kms.Key(
            self,
            "AgentCoreKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # The canonical AgentCore KMS key policy statements for the Gateway service role are
        # added below, AFTER the role is created. They follow the exact shape of the example
        # in https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-encryption.html
        # — DescribeKey + Decrypt + GenerateDataKey + CreateGrant with encryption-context
        # constraints. PolicyEngine grants are created on behalf of the deployer via FAS so
        # kms:CreateGrant on the account root is also required (per the canonical example in
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-encryption.html).
        table = dynamodb.Table(
            self,
            "SyntheticDataTable",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=data_key,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.DESTROY,
        )
        artifact_bucket = s3.Bucket(
            self,
            "ArtifactBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=data_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            auto_delete_objects=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        ui_bucket = s3.Bucket(
            self,
            "UiBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            auto_delete_objects=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        ui_origin_access_control = cloudfront.S3OriginAccessControl(
            self,
            "UiOriginAccessControl",
            origin_access_control_name=ui_oac_name,
        )
        ui_response_headers_policy = cloudfront.ResponseHeadersPolicy(
            self,
            "UiResponseHeadersPolicy",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self'; "
                        "script-src 'self'; "
                        "style-src 'self'; "
                        "img-src 'self' data:; "
                        f"connect-src 'self' https://*.execute-api.{self.region}.amazonaws.com "
                        f"https://*.auth.{self.region}.amazoncognito.com; "
                        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
                    ),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    override=True,
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True,
                    mode_block=True,
                    override=True,
                ),
            ),
        )
        distribution = cloudfront.Distribution(
            self,
            "UiDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    ui_bucket,
                    origin_access_control=ui_origin_access_control,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=ui_response_headers_policy,
            ),
            default_root_object="index.html",
        )
        ui_url = f"https://{distribution.distribution_domain_name}"

        lambda_env = {
            "TABLE_NAME": table.table_name,
            "ARTIFACT_BUCKET": artifact_bucket.bucket_name,
            "SAMPLE_AWS_REGION": self.region,
            "BEDROCK_MODEL_ID": bedrock_model_id,
            "CORS_ALLOWED_ORIGIN": ui_url,
        }
        code = lambda_.Code.from_asset(
            ".",
            exclude=[
                "cdk.out",
                ".git",
                "frontend/node_modules",
                "frontend/dist",
                ".venv",
                "__pycache__",
                ".pytest_cache",
            ],
        )

        task_role = iam.Role(
            self,
            "TaskLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        table.grant_read_data(task_role)
        artifact_bucket.grant_read(task_role)
        audit_role = iam.Role(
            self,
            "AuditLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        audit_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem"],
                resources=[table.table_arn],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["EXCEPTION#*"]}},
            )
        )
        data_key.grant_encrypt_decrypt(audit_role)
        invoke_role = iam.Role(
            self,
            "InvokeAgentCoreLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )

        normalize_fn = self._lambda("NormalizeExceptionFn", "src.lambda_tasks.handlers.normalize_exception_handler", code, task_role, lambda_env)
        scope_fn = self._lambda("ScopeAndSeverityFn", "src.lambda_tasks.handlers.scope_and_severity_handler", code, task_role, lambda_env)
        invoke_fn = self._lambda(
            "InvokeAgentCoreFn",
            "src.lambda_tasks.handlers.invoke_agentcore_handler",
            code,
            invoke_role,
            lambda_env,
            timeout=Duration.minutes(5),
            reserved_concurrent_executions=5,
        )
        validate_fn = self._lambda("ValidateOutputFn", "src.lambda_tasks.handlers.validate_output_handler", code, task_role, lambda_env)
        gate_fn = self._lambda("PolicyConfidenceFn", "src.lambda_tasks.handlers.policy_confidence_handler", code, task_role, lambda_env)
        route_fn = self._lambda("RouteEnrichedCaseFn", "src.lambda_tasks.handlers.route_enriched_case_handler", code, task_role, lambda_env)
        manual_fn = self._lambda("ManualTriageFn", "src.lambda_tasks.handlers.manual_triage_handler", code, task_role, lambda_env)
        escalate_fn = self._lambda("EscalateFn", "src.lambda_tasks.handlers.escalate_handler", code, task_role, lambda_env)
        audit_fn = self._lambda("RecordAuditStateFn", "src.lambda_tasks.handlers.record_audit_state_handler", code, audit_role, lambda_env)
        gateway_tool_fn = self._lambda("GatewayToolFn", "src.gateway_tools.handler.handler", code, task_role, lambda_env)

        state_machine = self._state_machine(
            normalize_fn,
            scope_fn,
            invoke_fn,
            validate_fn,
            gate_fn,
            route_fn,
            manual_fn,
            escalate_fn,
            audit_fn,
        )

        runtime_role = iam.Role(
            self,
            "AgentCoreRuntimeRole",
            # Canonical AgentCore Runtime trust policy per
            # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html#runtime-permissions-execution —
            # includes confused-deputy protection via aws:SourceAccount and aws:SourceArn.
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"},
                },
            ),
        )
        bedrock_invoke_resources = self._bedrock_invoke_resources(bedrock_model_id)
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=bedrock_invoke_resources,
            )
        )
        guardrail = bedrock.CfnGuardrail(
            self,
            "TriageGuardrail",
            name=guardrail_name,
            description="Guardrail for synthetic post-trade triage prompts and advisory outputs",
            blocked_input_messaging="The request was blocked by the post-trade triage guardrail.",
            blocked_outputs_messaging="The response was blocked by the post-trade triage guardrail.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=filter_type,
                        input_strength="MEDIUM",
                        output_strength="MEDIUM",
                    )
                    for filter_type in ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"]
                ]
                + [
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK",
                        input_strength="MEDIUM",
                        output_strength="NONE",
                    )
                ],
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="EMAIL", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="PHONE", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="NAME", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="ADDRESS", action="ANONYMIZE"),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type="US_SOCIAL_SECURITY_NUMBER", action="BLOCK"),
                ]
            ),
        )
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "TriageGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description="Initial sample guardrail version",
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn],
            )
        )
        # Canonical AgentCore Runtime execution role permissions per
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html:
        # CloudWatch Logs under /aws/bedrock-agentcore/runtimes/*, X-Ray traces, CloudWatch
        # metrics in the bedrock-agentcore namespace, and the workload-identity access tokens
        # the runtime uses to call AWS services on behalf of the agent.
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity-directory/default/workload-identity/{runtime_name}-*",
                ],
            )
        )
        # Let the runtime decrypt the CDK asset bucket object if CDK encrypts staging with CMK,
        # and read/write our artifact bucket and synthetic data table.
        data_key.grant_decrypt(runtime_role)
        # Scoped KMS access for reading the CDK staging S3 asset when the
        # bootstrap bucket uses customer-managed SSE-KMS encryption. Default CDK
        # bootstrap buckets do not need this extra statement. If your bootstrap
        # bucket uses SSE-KMS, set CDK_BOOTSTRAP_KMS_KEY_ARN to the exact key ARN;
        # deploy.sh fails closed for KMS-encrypted bootstrap buckets when it is
        # missing.
        bootstrap_kms_key_arn = os.environ.get("CDK_BOOTSTRAP_KMS_KEY_ARN", "")
        if bootstrap_kms_key_arn:
            runtime_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["kms:Decrypt", "kms:GenerateDataKey"],
                    resources=[bootstrap_kms_key_arn],
                    conditions={
                        "StringEquals": {"kms:ViaService": f"s3.{self.region}.amazonaws.com"},
                    },
                )
            )
        artifact_bucket.grant_read_write(runtime_role)

        # AgentCore Runtime direct code deployment: ZIP of pre-built ARM64 wheels.
        # deploy.sh builds build/runtime/ via `uv pip install --python-platform aarch64-manylinux_2_28`
        # and copies src/ + data/ into it before `cdk deploy`. CDK then zips and uploads.
        #
        # Defensive: if someone runs `cdk synth` directly without deploy.sh, the dir may
        # not exist yet. Create an empty placeholder so synth does not crash; the real
        # contents are assembled by deploy.sh before any actual deploy.
        runtime_build_dir = Path("build/runtime")
        if not runtime_build_dir.exists():
            runtime_build_dir.mkdir(parents=True, exist_ok=True)
            (runtime_build_dir / ".placeholder").write_text(
                "Populated by scripts/deploy.sh before 'cdk deploy'. See docs/DEPLOYMENT.md.\n",
                encoding="utf-8",
            )
        runtime_asset = s3_assets.Asset(
            self,
            "AgentCoreRuntimeSource",
            path="build/runtime",
            exclude=["__pycache__", "*.pyc", "**/__pycache__/**"],
        )
        runtime_asset.grant_read(runtime_role)

        policy_engine = CfnResource(
            self,
            "AgentCorePolicyEngineGeneratedName",
            type="AWS::BedrockAgentCore::PolicyEngine",
            properties={
                "Name": policy_engine_name,
                "Description": "Default-deny read-only policy engine for synthetic post-trade triage",
                "EncryptionKeyArn": agentcore_key.key_arn,
            },
        )
        policy_engine_arn = policy_engine.get_att("PolicyEngineArn").to_string()
        policy_engine_id = policy_engine.get_att("PolicyEngineId").to_string()

        gateway_role = iam.Role(
            self,
            "AgentCoreGatewayRole",
            # Canonical AgentCore Gateway trust policy per
            # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html —
            # the service assumes this role with explicit aws:SourceAccount and aws:SourceArn
            # conditions. Without these conditions the role can still be assumed but violates
            # confused-deputy protection.
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"},
                },
            ),
        )
        gateway_tool_fn.grant_invoke(gateway_role)
        # Policy in AgentCore Gateway execution role requires specific actions per
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html#gateway-execution-role:
        #   - GetPolicyEngine: lookup the attached policy engine at gateway creation/use
        #   - AuthorizeAction: evaluate Cedar policies on each tool call
        #   - PartiallyAuthorizeActions: list tools the caller is authorized for
        # Without these, gateway creation returns "Access denied while calling GetPolicyEngine",
        # policy attachment fails, or every tool invocation is denied by default.
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="PolicyEngineConfiguration",
                actions=["bedrock-agentcore:GetPolicyEngine"],
                # Sample trade-off: matches the AWS canonical example shape in
                # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html
                # which uses policy-engine/* because the policy-engine ID isn't yet known to
                # AgentCore at Gateway-creation time and specific ARNs produced "Policy Engine
                # Not Found" errors during first deploy. Narrow to the specific ARN in
                # production after first deploy; see docs/SECURITY.md.
                resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:policy-engine/*"],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="PolicyEngineAuthorization",
                actions=[
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                ],
                # Sample trade-off: AWS canonical example allows scoping these
                # actions to the policy-engine and gateway resource types. We keep
                # first-deploy wildcards inside this account/region because the IDs
                # are generated during creation; narrow in production once fixed.
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:policy-engine/*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                ],
            )
        )
        # AgentCore Gateway encrypts its internal state (e.g. target encryption configuration)
        # using the KMS key passed via KmsKeyArn. The canonical KMS setup for a Gateway with
        # customer-managed KMS is in
        # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-encryption.html
        # and requires BOTH identity-based (on the role) and resource-based (on the key)
        # permissions.

        # Identity-based: the Gateway role must list the KMS actions in its IAM policy.
        # Scoped to this specific key ARN.
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="GatewayKmsAccess",
                actions=[
                    "kms:DescribeKey",
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:CreateGrant",
                ],
                resources=[agentcore_key.key_arn],
            )
        )

        # Resource-based: the key policy must grant those same actions to the Gateway role,
        # constrained by kms:ViaService and encryption context per the AWS canonical example.
        # The EncryptionContext gateway-arn is published by AgentCore at Gateway creation as
        # a StringLike match. We can't know the gateway id at synth time, so we scope the
        # context to any gateway in this account/region, matching the AWS example where it
        # is set to the specific gateway ARN post-deploy. Narrow to the specific Gateway
        # ARN in production.
        via_service = f"bedrock-agentcore.{self.region}.amazonaws.com"
        gateway_arn_like = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
        agentcore_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowGatewayServiceRoleDescribeKey",
                principals=[iam.ArnPrincipal(gateway_role.role_arn)],
                actions=["kms:DescribeKey"],
                resources=["*"],
                conditions={"StringEquals": {"kms:ViaService": via_service}},
            )
        )
        agentcore_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowGatewayServiceRoleDecryptKey",
                principals=[iam.ArnPrincipal(gateway_role.role_arn)],
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=["*"],
                conditions={
                    "StringEquals": {"kms:ViaService": via_service},
                    "StringLike": {
                        "kms:EncryptionContext:aws:bedrock-agentcore-gateway:arn": gateway_arn_like,
                    },
                },
            )
        )
        agentcore_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowGatewayServiceRoleCreateGrant",
                principals=[iam.ArnPrincipal(gateway_role.role_arn)],
                actions=["kms:CreateGrant"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": via_service,
                        "kms:GrantConstraintType": "EncryptionContextSubset",
                    },
                    "ForAllValues:StringEquals": {
                        "kms:GrantOperations": ["Decrypt", "GenerateDataKey"],
                    },
                    "StringLike": {
                        "kms:EncryptionContext:aws:bedrock-agentcore-gateway:arn": gateway_arn_like,
                    },
                },
            )
        )
        # PolicyEngine encryption uses a FAS-based grant model: AgentCore calls kms:CreateGrant
        # using the deployer's credentials on behalf of the PolicyEngine. The canonical key
        # policy in https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-encryption.html
        # therefore grants kms:CreateGrant to the account root (not to a service principal).
        agentcore_key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowPolicyEngineCreateGrantViaFas",
                principals=[iam.AccountRootPrincipal()],
                actions=[
                    "kms:CreateGrant",
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:DescribeKey",
                ],
                resources=["*"],
                conditions={
                    "StringEquals": {"kms:ViaService": via_service},
                },
            )
        )
        gateway = CfnResource(
            self,
            "AgentCoreGatewayGeneratedName",
            type="AWS::BedrockAgentCore::Gateway",
            properties={
                "Name": gateway_name,
                "Description": "Read-only synthetic evidence tools for post-trade triage",
                "AuthorizerType": "AWS_IAM",
                "ProtocolType": "MCP",
                "RoleArn": gateway_role.role_arn,
                "KmsKeyArn": agentcore_key.key_arn,
                # Deploy-time decision: the Gateway is created WITHOUT PolicyEngineConfiguration.
                # Attaching a PolicyEngine at Gateway-creation time via CloudFormation fails with
                # "Access denied while calling GetPolicyEngine" even when the Gateway execution
                # role has the canonical permissions from
                # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-permissions.html
                # — both with specific ARNs and with the canonical policy-engine/* wildcards.
                # The failure is service-side (IAM simulate shows the role policy is correct),
                # likely a race between PolicyEngine ACTIVE status and the Gateway's
                # GetPolicyEngine call at creation time.
                #
                # Workaround for the sample: create the Gateway first, then run
                # scripts/configure-policy.sh to attach the PolicyEngine, create the
                # Cedar policies, and move the Gateway to ENFORCE after validation.
                # See docs/DEPLOYMENT.md.
            },
        )
        # CDK attaches add_to_policy statements via a separate AWS::IAM::Policy resource. The
        # Gateway's internal encryption call races that attachment at creation time, so we
        # explicitly force the Gateway to wait for the role's default policy (which includes
        # lambda:InvokeFunction, the KMS statements, and the policy-engine authorization
        # actions) before the Gateway is created. Without this explicit dependency,
        # CloudFormation can schedule the Gateway's CreateGateway call before the inline
        # policy has been attached to the role, and the Gateway service returns
        # "no identity-based policy allows the kms:GenerateDataKey action".
        gateway_default_policy = gateway_role.node.try_find_child("DefaultPolicy")
        if gateway_default_policy is not None:
            gateway.add_dependency(gateway_default_policy.node.default_child)
        gateway.add_dependency(policy_engine)
        gateway_arn = gateway.get_att("GatewayArn").to_string()
        gateway_url = gateway.get_att("GatewayUrl").to_string()
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokePostTradeGateway",
                actions=["bedrock-agentcore:InvokeGateway"],
                resources=[gateway_arn],
            )
        )

        CfnResource(
            self,
            "AgentCoreGatewayTargetGeneratedName",
            type="AWS::BedrockAgentCore::GatewayTarget",
            properties={
                "GatewayIdentifier": gateway.get_att("GatewayIdentifier").to_string(),
                "Name": gateway_target_name,
                "Description": "Read-only Lambda-backed MCP tools over synthetic datasets",
                "CredentialProviderConfigurations": [{"CredentialProviderType": "GATEWAY_IAM_ROLE"}],
                "TargetConfiguration": {
                    "Mcp": {
                        "Lambda": {
                            "LambdaArn": gateway_tool_fn.function_arn,
                            "ToolSchema": {"InlinePayload": self._tool_definitions()},
                        }
                    }
                },
            },
        )

        # The Cedar policies are NOT declared in this stack. Per the AWS getting-started
        # tutorial (https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-getting-started.html):
        #   "Cedar policies that reference specific gateway ARNs in the resource field
        #    require a two-phase deployment: first deploy without the policy to create the
        #    gateway, then retrieve the gateway ARN from agentcore status, update the Cedar
        #    file, and add the policy before redeploying."
        #
        # Additionally, AgentCore auto-generates the Cedar validation schema from the
        # Gateway's MCP tool manifest (the GatewayTarget). When AWS::BedrockAgentCore::Policy
        # is created in the same stack as the GatewayTarget, the policy resource can race
        # the GatewayTarget's tool registration and fail stabilization.
        #
        # Workaround for the sample: create the Cedar policies post-deploy with
        # scripts/configure-policy.sh. Keeping the policy statements outside the
        # stack also makes the generated Gateway ARN explicit for readers.

        runtime = CfnResource(
            self,
            "AgentCoreRuntimeGeneratedName",
            type="AWS::BedrockAgentCore::Runtime",
            properties={
                "AgentRuntimeName": runtime_name,
                "Description": "Strands Agents four-stage advisory post-trade triage runtime",
                "RoleArn": runtime_role.role_arn,
                "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
                "ProtocolConfiguration": "HTTP",
                "AgentRuntimeArtifact": {
                    "CodeConfiguration": {
                        "Code": {
                            "S3": {
                                "Bucket": runtime_asset.s3_bucket_name,
                                "Prefix": runtime_asset.s3_object_key,
                            }
                        },
                        "EntryPoint": ["src/agentcore_runtime/app.py"],
                        "Runtime": "PYTHON_3_12",
                    },
                },
                "EnvironmentVariables": {
                    "ARTIFACT_BUCKET": artifact_bucket.bucket_name,
                    "SAMPLE_AWS_REGION": self.region,
                    "BEDROCK_MODEL_ID": bedrock_model_id,
                    "BEDROCK_GUARDRAIL_ID": guardrail.attr_guardrail_id,
                    "BEDROCK_GUARDRAIL_VERSION": guardrail_version.attr_version,
                    "GATEWAY_IDENTIFIER": gateway.get_att("GatewayIdentifier").to_string(),
                    "AGENTCORE_GATEWAY_URL": gateway_url,
                    "AGENTCORE_GATEWAY_REQUIRED": "1",
                    "GATEWAY_TARGET_NAME": gateway_target_name,
                },
            },
        )
        runtime_arn = runtime.get_att("AgentRuntimeArn").to_string()
        # InvokeAgentRuntime authorizes against the concrete runtime endpoint
        # ARN when a qualifier such as DEFAULT is used.
        runtime_default_endpoint_arn = Fn.join("", [runtime_arn, "/runtime-endpoint/DEFAULT"])
        runtime_invoke_resources = [runtime_arn, runtime_default_endpoint_arn]
        invoke_fn.add_environment("AGENT_RUNTIME_ARN", runtime_arn)
        invoke_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"],
                resources=runtime_invoke_resources,
            )
        )

        evaluator = None
        if enable_evaluator:
            evaluator = CfnResource(
                self,
                "AgentCoreEvaluatorGeneratedName",
                type="AWS::BedrockAgentCore::Evaluator",
                properties={
                    "EvaluatorName": evaluator_name,
                    "Description": "LLM-as-judge evaluator for advisory triage output quality",
                    "Level": "SESSION",
                    "EvaluatorConfig": {
                        "LlmAsAJudge": {
                            # AgentCore requires SESSION-level instructions to reference at least one
                            # of: {available_tools}, {context}, {actual_tool_trajectory},
                            # {expected_tool_trajectory}, {assertions}. We include the three that are
                            # meaningful for this advisory triage flow: the session context so the
                            # judge sees the case payload, and the tool trajectory so it can verify
                            # only read-only evidence tools were called.
                            "Instructions": (
                                "You are evaluating an advisory post-trade exception triage session. "
                                "Score whether the session returned a bounded advisory recommendation "
                                "that is grounded in evidence and requires human approval.\n\n"
                                "Session context:\n{context}\n\n"
                                "Tools the agent could call: {available_tools}\n\n"
                                "Tools the agent actually called: {actual_tool_trajectory}\n\n"
                                "Return Good if the recommendation is grounded in the evidence, is "
                                "advisory (requires human approval), and the agent only used read-only "
                                "evidence tools. Return Poor otherwise."
                            ),
                            "ModelConfig": {
                                "BedrockEvaluatorModelConfig": {
                                    "ModelId": evaluator_model_id,
                                    "InferenceConfig": {"MaxTokens": 512, "Temperature": 0.0},
                                }
                            },
                            "RatingScale": {
                                "Numerical": [
                                    {"Label": "Poor", "Value": 0, "Definition": "Missing required advisory or evidence controls"},
                                    {"Label": "Good", "Value": 1, "Definition": "Grounded recommendation with policy and human approval controls"},
                                ]
                            },
                        }
                    },
                },
            )

        user_pool = cognito.UserPool(
            self,
            "DemoAuthUserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True),
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_lowercase=True,
                require_symbols=True,
                require_uppercase=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        user_pool_client = cognito.UserPoolClient(
            self,
            "DemoAuthUserPoolClient",
            user_pool=user_pool,
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(implicit_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=[ui_url],
                logout_urls=[ui_url],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )
        demo_auth_domain_prefix = Fn.join("-", ["post-trade-demo", Aws.ACCOUNT_ID, stack_guid_compact])
        cognito.CfnUserPoolDomain(
            self,
            "DemoAuthUserPoolDomain",
            domain=demo_auth_domain_prefix,
            user_pool_id=user_pool.user_pool_id,
        )
        hosted_ui_domain = Fn.join(
            "",
            ["https://", demo_auth_domain_prefix, ".auth.", self.region, ".amazoncognito.com"],
        )
        demo_auth_authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "DemoAuthAuthorizer",
            cognito_user_pools=[user_pool],
        )
        demo_auth_method_options = apigw.MethodOptions(
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorizer=demo_auth_authorizer,
        )
        demo_auth_config: dict[str, str | bool] = {
            "enabled": True,
            "userPoolId": user_pool.user_pool_id,
            "userPoolClientId": user_pool_client.user_pool_client_id,
            "hostedUiDomain": hosted_ui_domain,
            "redirectUri": ui_url,
            "logoutUri": ui_url,
        }

        # The UI API Lambda gets its own IAM role so it can reference the state
        # machine ARN without creating a circular dependency. Every other task
        # Lambda shares `task_role`; if we attached Step Functions permissions
        # there, CFN would see role -> state_machine -> task_lambdas -> role and
        # refuse to build the change set.
        ui_api_role = iam.Role(
            self,
            "UiApiRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")],
        )
        # /cases and /cases/{id} read synthetic data; /evaluation reads the
        # EVALUATION#LATEST item; POST /executions writes no DDB.
        table.grant_read_data(ui_api_role)
        artifact_bucket.grant_read(ui_api_role)
        # StartExecution on the state machine; DescribeExecution and
        # GetExecutionHistory on any execution belonging to it.
        ui_api_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[state_machine.state_machine_arn],
            )
        )
        ui_api_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution", "states:GetExecutionHistory"],
                resources=[f"arn:aws:states:{self.region}:{self.account}:execution:{state_machine.state_machine_name}:*"],
            )
        )
        ui_api_env = {
            **lambda_env,
            "STATE_MACHINE_ARN": state_machine.state_machine_arn,
            "COGNITO_ALLOWED_CLIENT_ID": str(demo_auth_config.get("userPoolClientId", "")),
        }
        api_fn = self._lambda(
            "UiApiFn",
            "src.lambda_tasks.handlers.ui_api_handler",
            code,
            ui_api_role,
            ui_api_env,
            reserved_concurrent_executions=10,
        )
        api = apigw.LambdaRestApi(
            self,
            "TriageApi",
            handler=api_fn,
            proxy=True,
            deploy_options=apigw.StageOptions(
                stage_name=api_stage_name,
                throttling_rate_limit=10,
                throttling_burst_limit=20,
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=[ui_url],
                allow_methods=apigw.Cors.ALL_METHODS,
                allow_headers=["content-type", "authorization"],
            ),
            default_method_options=demo_auth_method_options,
        )
        api_waf_log_group = logs.LogGroup(
            self,
            "ApiWafLogGroup",
            log_group_name=Fn.join("-", ["aws-waf-logs", Aws.STACK_NAME, stack_guid_compact, "api"]),
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        api_waf_visibility = {
            "CloudWatchMetricsEnabled": True,
            "MetricName": "PostTradeApiWebAcl",
            "SampledRequestsEnabled": True,
        }
        api_web_acl = CfnResource(
            self,
            "ApiWebAcl",
            type="AWS::WAFv2::WebACL",
            properties={
                "DefaultAction": {"Allow": {}},
                "Description": "Regional WAF for the synthetic post-trade triage API",
                "Scope": "REGIONAL",
                "VisibilityConfig": api_waf_visibility,
                "Rules": [
                    {
                        "Name": "AWSManagedRulesCommonRuleSet",
                        "Priority": 0,
                        "OverrideAction": {"None": {}},
                        "Statement": {
                            "ManagedRuleGroupStatement": {
                                "VendorName": "AWS",
                                "Name": "AWSManagedRulesCommonRuleSet",
                            }
                        },
                        "VisibilityConfig": {
                            "CloudWatchMetricsEnabled": True,
                            "MetricName": "AWSManagedRulesCommonRuleSet",
                            "SampledRequestsEnabled": True,
                        },
                    },
                    {
                        "Name": "AWSManagedRulesAmazonIpReputationList",
                        "Priority": 1,
                        "OverrideAction": {"None": {}},
                        "Statement": {
                            "ManagedRuleGroupStatement": {
                                "VendorName": "AWS",
                                "Name": "AWSManagedRulesAmazonIpReputationList",
                            }
                        },
                        "VisibilityConfig": {
                            "CloudWatchMetricsEnabled": True,
                            "MetricName": "AWSManagedRulesAmazonIpReputationList",
                            "SampledRequestsEnabled": True,
                        },
                    },
                    {
                        "Name": "PerIpRateLimit",
                        "Priority": 2,
                        "Action": {"Block": {}},
                        "Statement": {
                            "RateBasedStatement": {
                                "AggregateKeyType": "IP",
                                "Limit": 1000,
                            }
                        },
                        "VisibilityConfig": {
                            "CloudWatchMetricsEnabled": True,
                            "MetricName": "PerIpRateLimit",
                            "SampledRequestsEnabled": True,
                        },
                    },
                ],
            },
        )
        api_stage_arn = Fn.join(
            "",
            [
                "arn:",
                Aws.PARTITION,
                ":apigateway:",
                self.region,
                "::/restapis/",
                api.rest_api_id,
                "/stages/",
                api.deployment_stage.stage_name,
            ],
        )
        api_web_acl_association = CfnResource(
            self,
            "ApiWebAclAssociation",
            type="AWS::WAFv2::WebACLAssociation",
            properties={
                "ResourceArn": api_stage_arn,
                "WebACLArn": api_web_acl.get_att("Arn").to_string(),
            },
        )
        api_web_acl_logging = CfnResource(
            self,
            "ApiWebAclLogging",
            type="AWS::WAFv2::LoggingConfiguration",
            properties={
                "LogDestinationConfigs": [api_waf_log_group.log_group_arn],
                "RedactedFields": [
                    {"SingleHeader": {"Name": "authorization"}},
                ],
                "ResourceArn": api_web_acl.get_att("Arn").to_string(),
            },
        )

        # Streaming Lambda (feature: responseStreaming). Adds the
        # GET /executions/stream?case_key=<key> route to the existing TriageApi
        # REST API with ResponseTransferMode.STREAM so the browser sees live
        # stage-by-stage SSE events as the AgentCore Runtime produces them.
        #
        # See docs/DEPLOYMENT.md "Response Streaming (feature flag)" for the
        # full trade-off write-up, including the per-demo double-invocation
        # cost warning.
        streaming_endpoint_url: str | None = None
        if self._response_streaming_enabled:
            # Package the streaming Lambda's ZIP root, assembled by deploy.sh
            # into build/streaming_invoke/ with ARM64 wheels + our own modules.
            streaming_build_dir = Path("build/streaming_invoke")
            if not streaming_build_dir.exists():
                streaming_build_dir.mkdir(parents=True, exist_ok=True)
                (streaming_build_dir / ".placeholder").write_text(
                    "Populated by scripts/deploy.sh before 'cdk deploy'. See docs/DEPLOYMENT.md.\n",
                    encoding="utf-8",
                )
            streaming_code = lambda_.Code.from_asset(
                "build/streaming_invoke",
                exclude=["__pycache__", "*.pyc"],
            )

            streaming_role = iam.Role(
                self,
                "StreamingInvokeRole",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
                ],
            )
            # Narrow: the streaming Lambda only needs to invoke the single
            # AgentCore Runtime. No DDB, no SFN, no S3.
            streaming_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"],
                    resources=runtime_invoke_resources,
                )
            )
            table.grant_read_data(streaming_role)

            # AWS Lambda Web Adapter (LWA) as a managed layer. The 753240598075
            # account is the canonical public LWA publisher.
            # See https://aws.github.io/aws-lambda-web-adapter/getting-started/zip-packages.html
            lwa_layer = lambda_.LayerVersion.from_layer_version_arn(
                self,
                "LambdaAdapterLayerArm64",
                f"arn:aws:lambda:{self.region}:753240598075:layer:LambdaAdapterLayerArm64:27",
            )

            streaming_log_group = logs.LogGroup(
                self,
                "StreamingInvokeFnLogGroup",
                retention=logs.RetentionDays.ONE_WEEK,
                removal_policy=RemovalPolicy.DESTROY,
            )
            streaming_fn = lambda_.Function(
                self,
                "StreamingInvokeFn",
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                code=streaming_code,
                # Handler is the LWA startup script, not a Python function.
                # LWA reads AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap, probes the
                # web app's readiness, then forwards Lambda invocations.
                handler="src/streaming_invoke/run.sh",
                role=streaming_role,
                timeout=Duration.minutes(15),  # max: matches API Gateway streaming max timeout
                memory_size=1024,
                reserved_concurrent_executions=3,
                layers=[lwa_layer],
                log_group=streaming_log_group,
                environment={
                    **lambda_env,
                    "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                    "AWS_LWA_INVOKE_MODE": "response_stream",
                    "AWS_LWA_PORT": "8080",
                    "AWS_LWA_READINESS_CHECK_PATH": "/ping",
                    "AGENT_RUNTIME_ARN": runtime_arn,
                    "AGENT_RUNTIME_QUALIFIER": "DEFAULT",
                    "PYTHONPATH": "/var/task",
                },
            )

            # Add GET /executions/stream with STREAM transfer mode. Placed
            # before the API Gateway catch-all proxy route (which is handled
            # by api_fn) so this explicit route takes priority.
            executions_resource = api.root.add_resource("executions-stream")
            executions_resource.add_method(
                "GET",
                apigw.LambdaIntegration(
                    streaming_fn,
                    response_transfer_mode=apigw.ResponseTransferMode.STREAM,
                    # Streaming integration keeps the connection alive up to
                    # the integration timeout (max 29s for non-streaming,
                    # extended to 15 min for streaming).
                    timeout=Duration.seconds(900),
                ),
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorizer=demo_auth_authorizer,
            )
            streaming_endpoint_url = f"{api.url}executions-stream"

        dashboard = cloudwatch.Dashboard(self, "Dashboard")
        execution_volume_alarm = cloudwatch.Alarm(
            self,
            "ExecutionVolumeAlarm",
            metric=state_machine.metric_started(period=Duration.hours(1), statistic="Sum"),
            threshold=100,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description=(
                "Synthetic post-trade demo execution volume exceeded 100 Step Functions starts in one hour. "
                "Review for accidental loops or unauthorized use."
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Step Functions Executions",
                left=[
                    state_machine.metric_started(),
                    state_machine.metric_succeeded(),
                    state_machine.metric_failed(),
                ],
            )
        )

        # Ship a runtime config next to the static bundle so the UI build is
        # environment-agnostic. The frontend loads /config.js before mounting
        # and reads window.__TRIAGE_CONFIG.apiUrl to know where to POST.
        #
        # Also surfaces the responseStreaming feature flag and (when enabled)
        # the streaming endpoint URL so the UI can open a text/event-stream
        # fetch without having to guess either value.
        streaming_endpoint_js = (
            f"'{streaming_endpoint_url}'" if streaming_endpoint_url else "null"
        )
        response_streaming_js = "true" if self._response_streaming_enabled else "false"
        demo_auth_js = (
            "{"
            " enabled: true,"
            f" userPoolId: '{demo_auth_config['userPoolId']}',"
            f" userPoolClientId: '{demo_auth_config['userPoolClientId']}',"
            f" hostedUiDomain: '{demo_auth_config['hostedUiDomain']}',"
            f" redirectUri: '{demo_auth_config['redirectUri']}',"
            f" logoutUri: '{demo_auth_config['logoutUri']}'"
            " }"
        )
        s3deploy.BucketDeployment(
            self,
            "DeployUi",
            sources=[
                s3deploy.Source.asset("frontend/dist"),
                s3deploy.Source.data(
                    "config.js",
                    (
                        "window.__TRIAGE_CONFIG = {"
                        f" apiUrl: '{api.url}',"
                        f" awsRegion: '{self.region}',"
                        f" dashboardName: '{dashboard.dashboard_name}',"
                        f" responseStreaming: {response_streaming_js},"
                        f" streamingEndpointUrl: {streaming_endpoint_js},"
                        f" auth: {demo_auth_js}"
                        " };"
                    ),
                ),
            ],
            destination_bucket=ui_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "DynamoTableName", value=table.table_name)
        CfnOutput(self, "ArtifactBucketName", value=artifact_bucket.bucket_name)
        CfnOutput(self, "UiBucketName", value=ui_bucket.bucket_name)
        CfnOutput(self, "CloudscapeUiUrl", value=ui_url)
        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "AgentCoreRuntimeArn", value=runtime.get_att("AgentRuntimeArn").to_string())
        CfnOutput(self, "AgentCoreGatewayId", value=gateway.get_att("GatewayIdentifier").to_string())
        CfnOutput(self, "AgentCoreGatewayArn", value=gateway_arn)
        CfnOutput(self, "AgentCoreGatewayUrl", value=gateway_url)
        CfnOutput(self, "AgentCoreGatewayTargetName", value=gateway_target_name)
        CfnOutput(
            self,
            "ResponseStreamingEnabled",
            value="true" if self._response_streaming_enabled else "false",
        )
        if streaming_endpoint_url is not None:
            CfnOutput(self, "StreamingEndpointUrl", value=streaming_endpoint_url)
        CfnOutput(self, "AgentCorePolicyEngineId", value=policy_engine_id)
        CfnOutput(self, "AgentCorePolicyEngineArn", value=policy_engine_arn)
        CfnOutput(self, "AgentCoreEvaluatorEnabled", value="true" if enable_evaluator else "false")
        if evaluator is not None:
            CfnOutput(self, "AgentCoreEvaluatorArn", value=evaluator.ref)
            CfnOutput(self, "AgentCoreEvaluatorModelId", value=evaluator_model_id)
        CfnOutput(self, "DemoAuthEnabled", value="true")
        CfnOutput(self, "DemoAuthUserPoolId", value=str(demo_auth_config["userPoolId"]))
        CfnOutput(self, "DemoAuthUserPoolClientId", value=str(demo_auth_config["userPoolClientId"]))
        CfnOutput(self, "DemoAuthHostedUiDomain", value=str(demo_auth_config["hostedUiDomain"]))
        CfnOutput(self, "CloudWatchDashboardName", value=dashboard.dashboard_name)
        CfnOutput(self, "ExecutionVolumeAlarmName", value=execution_volume_alarm.alarm_name)

        apply_nag_suppressions(self)

    def _lambda(
        self,
        construct_id: str,
        handler: str,
        code: lambda_.Code,
        role: iam.Role,
        environment: dict[str, str],
        timeout: Duration = Duration.seconds(30),
        reserved_concurrent_executions: int | None = None,
    ) -> lambda_.Function:
        log_group = logs.LogGroup(
            self,
            f"{construct_id}LogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return lambda_.Function(
            self,
            construct_id,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler=handler,
            code=code,
            role=role,
            timeout=timeout,
            memory_size=512,
            reserved_concurrent_executions=reserved_concurrent_executions,
            environment=environment,
            log_group=log_group,
        )

    def _state_machine(self, normalize_fn, scope_fn, invoke_fn, validate_fn, gate_fn, route_fn, manual_fn, escalate_fn, audit_fn) -> sfn.StateMachine:
        normalize = tasks.LambdaInvoke(self, "Normalize exception", lambda_function=normalize_fn, payload_response_only=True)
        scope = tasks.LambdaInvoke(self, "Scope and severity", lambda_function=scope_fn, payload_response_only=True)
        invoke = tasks.LambdaInvoke(self, "Invoke AgentCore", lambda_function=invoke_fn, payload_response_only=True)
        validate = tasks.LambdaInvoke(self, "Validate output", lambda_function=validate_fn, payload_response_only=True)
        gate = tasks.LambdaInvoke(self, "Policy / confidence met?", lambda_function=gate_fn, payload_response_only=True)
        route = tasks.LambdaInvoke(self, "Route enriched case", lambda_function=route_fn, payload_response_only=True)
        manual = tasks.LambdaInvoke(self, "Manual triage", lambda_function=manual_fn, payload_response_only=True)
        escalate = tasks.LambdaInvoke(self, "Escalate", lambda_function=escalate_fn, payload_response_only=True)
        audit = tasks.LambdaInvoke(self, "Record audit state", lambda_function=audit_fn, payload_response_only=True)
        attach_execution_context = sfn.Pass(
            self,
            "Attach execution context",
            parameters={
                "execution_arn.$": "$$.Execution.Id",
                "case.$": "$.case",
                "normalization.$": "$.normalization",
            },
        )
        manual.next(audit)
        route.next(audit)
        escalate.next(audit)
        eligible = sfn.Choice(self, "Eligible?")
        post_gate = sfn.Choice(self, "Route decision")
        definition = (
            normalize.next(attach_execution_context).next(scope).next(
                eligible.when(sfn.Condition.boolean_equals("$.scope.eligible", True), invoke.next(validate).next(gate).next(post_gate))
                .otherwise(escalate)
            )
        )
        post_gate.when(sfn.Condition.string_equals("$.gate.decision", "ROUTE_ENRICHED_CASE"), route)
        post_gate.when(sfn.Condition.string_equals("$.gate.decision", "ESCALATE"), escalate)
        post_gate.otherwise(manual)
        return sfn.StateMachine(
            self,
            "AgentAssistedTriageControlLayer",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            state_machine_type=sfn.StateMachineType.STANDARD,
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(self, "WorkflowLogs", retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.DESTROY),
                level=sfn.LogLevel.ALL,
            ),
        )

    def _tool_definitions(self) -> list[dict]:
        object_schema = {"Type": "object", "Properties": {}, "Required": []}
        return [
            self._tool("get_trade_details", "Read synthetic trade details by exception_id", ["exception_id"], object_schema),
            self._tool("get_settlement_status", "Read synthetic settlement status by exception_id", ["exception_id"], object_schema),
            self._tool("get_allocation_status", "Read synthetic allocation status by exception_id", ["exception_id"], object_schema),
            self._tool("get_ssi_record", "Read synthetic SSI record by exception-scoped counterparty_id and account_id", ["exception_id", "counterparty_id", "account_id"], object_schema),
            self._tool("search_prior_cases", "Search synthetic prior cases by exception-scoped root_cause_category", ["exception_id", "root_cause_category"], object_schema),
            self._tool("get_playbook", "Read an approved synthetic playbook by exception-scoped playbook_id", ["exception_id", "playbook_id"], object_schema),
        ]

    def _tool(self, name: str, description: str, required: list[str], output_schema: dict, optional: list[str] | None = None) -> dict:
        properties = {field: {"Type": "string"} for field in required + (optional or [])}
        return {
            "Name": name,
            "Description": description,
            "InputSchema": {"Type": "object", "Properties": properties, "Required": required},
            "OutputSchema": output_schema,
        }

    def _bedrock_invoke_resources(self, model_id: str) -> list[str]:
        if model_id.startswith("arn:"):
            return [model_id]

        resources = {
            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}",
            f"arn:aws:bedrock:{self.region}:{self.account}:application-inference-profile/{model_id}",
        }
        foundation_model_id = re.sub(r"^(us|eu|apac|global)\.", "", model_id)
        resources.update(
            {
                f"arn:aws:bedrock:{self.region}::foundation-model/{foundation_model_id}*",
            }
        )
        return sorted(resources)

    def _generated_resource_name(
        self,
        seed_id: str,
        prefix: str,
        *,
        max_length: int = 48,
        separator: str = "_",
    ) -> str:
        marker = Construct(self, f"{seed_id}NameSeed")
        suffix = re.sub(r"[^A-Za-z0-9]", "", Names.unique_id(marker))[-12:]
        base = re.sub(r"[^A-Za-z0-9]", "", prefix) if separator == "_" else re.sub(r"[^A-Za-z0-9-]", "", prefix)
        if not base or not base[0].isalpha():
            base = f"A{base}"
        candidate = f"{base}{separator}{suffix}" if separator else f"{base}{suffix}"
        if len(candidate) <= max_length:
            return candidate
        keep = max_length - len(separator) - len(suffix)
        return f"{base[:keep]}{separator}{suffix}"
