from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_demo_auth_is_required_without_public_self_registration() -> None:
    context = json.loads((ROOT / "cdk.json").read_text(encoding="utf-8"))["context"]
    stack = _read("infra/triage_stack.py")
    frontend = _read("frontend/src/auth.js")
    deploy_script = _read("scripts/deploy.sh")
    setup_script = _read("scripts/setup-demo.sh")

    assert ("demo" + "Auth") not in context
    assert ("demo" + "_auth:") not in stack
    assert ("self._demo" + "_auth_enabled") not in stack
    assert ("DEMO" + "_AUTH") not in deploy_script
    assert ("DEMO" + "_AUTH") not in setup_script
    assert ("I_UNDERSTAND" + "_NO_AUTH") not in deploy_script
    assert ("I_UNDERSTAND" + "_NO_AUTH") not in setup_script
    assert "CognitoUserPoolsAuthorizer" in stack
    assert "default_method_options=demo_auth_method_options" in stack
    assert "DemoAuthHostedUiDomain" in stack
    assert "self_sign_up_enabled=False" in stack
    assert "user_password=True" not in stack
    assert "completeHostedUiSignIn" in frontend
    assert "return !isAuthEnabled()" not in frontend
    assert "Cognito Hosted UI is not configured for this protected demo." in frontend
    assert ("export function " + "signUp") not in frontend
    assert ('authUrl("/' + 'signup"') not in frontend


def test_demo_auth_landing_page_hides_operational_content_until_sign_in() -> None:
    frontend = _read("frontend/src/main.jsx")
    index = _read("frontend/index.html")
    bootstrap = _read("frontend/src/bootstrap.jsx")

    assert "const canUseDemo = hasApi && authEnabled && signedIn;" in frontend
    assert "const showOperationalLinks = canUseDemo;" in frontend
    assert "Authentication is not configured" in frontend
    assert '<script src="/config.js"></script>' not in index
    assert 'script.src = "/config.js";' in bootstrap
    assert 'import("./main.jsx")' in bootstrap
    assert "showOperationalLinks ? (" in frontend
    assert ("Create " + "account") not in frontend
    assert "provisioned demo user" in frontend
    assert "{canUseDemo ? <EvaluationMetrics metrics={metrics} /> : null}" in frontend
    assert "{canUseDemo ? (" in frontend
    assert "Inspect workflow" in frontend


def test_public_api_has_mandatory_auth_restricted_cors_and_regional_waf() -> None:
    stack = _read("infra/triage_stack.py")
    handler = _read("src/lambda_tasks/handlers.py")
    streaming = _read("src/streaming_invoke/app.py")
    frontend_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "frontend" / "src").glob("*.jsx"))
    )

    assert '"AWS::WAFv2::WebACL"' in stack
    assert '"AWS::WAFv2::WebACLAssociation"' in stack
    assert '"AWS::WAFv2::LoggingConfiguration"' in stack
    assert "ApiWebAcl" in stack
    assert "AWSManagedRulesCommonRuleSet" in stack
    assert "AWSManagedRulesAmazonIpReputationList" in stack
    assert "RateBasedStatement" in stack
    assert '"Limit": 1000' in stack
    assert '"AggregateKeyType": "IP"' in stack
    assert 'Fn.join("-", ["aws-waf-logs", Aws.STACK_NAME, stack_guid_compact, "api"])' in stack
    assert '"SingleHeader": {"Name": "authorization"}' in stack
    assert '"/stages/"' in stack
    assert "default_method_options=demo_auth_method_options" in stack
    assert "throttling_rate_limit=10" in stack
    assert "reserved_concurrent_executions=10" in stack
    assert "ExecutionVolumeAlarm" in stack
    assert "allow_origins=[ui_url]" in stack
    assert "Cors.ALL_ORIGINS" not in stack
    assert "Content-Security-Policy" in stack or "content_security_policy" in stack
    unsafe_inline = "unsafe" + "-inline"
    assert unsafe_inline not in stack
    assert "style={{" not in frontend_sources
    assert "dangerouslySetInnerHTML" not in frontend_sources
    assert 'os.environ.get("CORS_ALLOWED_ORIGIN", "")' in handler
    assert 'os.environ.get("CORS_ALLOWED_ORIGIN", "")' in streaming
    assert '"content-type,authorization"' in handler
    assert '"content-type,authorization"' in streaming


def test_agentcore_runtime_invocation_is_not_account_wildcard() -> None:
    stack = _read("infra/triage_stack.py")
    forbidden = (
        'actions=["bedrock-agentcore:InvokeAgentRuntime", '
        '"bedrock-agentcore:InvokeAgentRuntimeForUser"],\n'
        '                resources=["*"],'
    )

    assert forbidden not in stack
    assert 'runtime_default_endpoint_arn = Fn.join("", [runtime_arn, "/runtime-endpoint/DEFAULT"])' in stack
    assert "runtime_invoke_resources = [runtime_arn, runtime_default_endpoint_arn]" in stack
    assert "resources=runtime_invoke_resources" in stack
    assert "arn:aws:bedrock:*::foundation-model" not in stack
    assert "arn:aws:bedrock:::foundation-model" not in stack


def test_required_physical_names_are_stack_guid_scoped() -> None:
    stack = _read("infra/triage_stack.py")

    assert 'stack_guid = Fn.select(2, Fn.split("/", Aws.STACK_ID))' in stack
    assert 'stack_guid_compact = Fn.join("", Fn.split("-", stack_guid))' in stack
    assert 'policy_engine_name = Fn.join("_", ["PostTradePE", stack_guid_compact])' in stack
    assert 'gateway_name = Fn.join("-", ["post-trade-gw", stack_guid_compact])' in stack
    assert 'gateway_target_name = Fn.join("-", ["post-trade-tools", stack_guid_compact])' in stack
    assert 'runtime_name = Fn.join("_", ["PostTradeRT", stack_guid_compact])' in stack
    assert 'evaluator_name = Fn.join("_", ["PostTradeEval", stack_guid_compact])' in stack
    assert 'guardrail_name = Fn.join("-", ["PostTradeGR", stack_guid_compact])' in stack
    assert 'demo_auth_domain_prefix = Fn.join("-", ["post-trade-demo", Aws.ACCOUNT_ID, stack_guid_compact])' in stack
    assert 'self._generated_resource_name("PolicyEngine"' not in stack
    assert 'self._generated_resource_name("Gateway"' not in stack
    assert 'self._generated_resource_name("GatewayTarget"' not in stack
    assert 'self._generated_resource_name("Runtime"' not in stack
    assert 'self._generated_resource_name("Evaluator"' not in stack
    assert 'self._generated_resource_name("Guardrail"' not in stack
    assert "DemoAuthDomainSeed" not in stack


def test_iam_actions_match_documented_bedrock_and_agentcore_permissions() -> None:
    stack = _read("infra/triage_stack.py")
    security = _read("docs/SECURITY.md")
    deploy_script = _read("scripts/deploy.sh")

    assert '"bedrock:InvokeModel"' in stack
    assert '"bedrock:InvokeModelWithResponseStream"' in stack
    assert "bedrock:Converse" not in stack
    assert "bedrock:Converse" not in security
    assert '"bedrock-agentcore:GetPolicyEngine"' in stack
    assert '"bedrock-agentcore:AuthorizeAction"' in stack
    assert '"bedrock-agentcore:PartiallyAuthorizeActions"' in stack
    assert "CheckAuthorizePermissions" not in stack
    assert "CheckAuthorizePermissions" not in security
    assert "/policy-engines/*/target-resource/*" not in stack
    assert "/policy-engines/*/target-resource/*" not in security
    kms_resource_aliases = "kms:Resource" + "Aliases"
    bootstrap_resource_var = "bootstrap_kms" + "_resources"
    assert kms_resource_aliases not in stack
    assert f"resources={bootstrap_resource_var}" not in stack
    assert 'resources=["*"] if bootstrap_kms_key_arn else' not in stack
    assert 'resources=[bootstrap_kms_key_arn]' in stack
    assert 'CDK_BOOTSTRAP_KMS_KEY_ARN' in stack
    assert 'CDK_BOOTSTRAP_KMS_KEY_ARN' in deploy_script
    assert 'SSEAlgorithm' in deploy_script
    assert 'uses ${BOOTSTRAP_SSE_ALGORITHM} encryption' in deploy_script


def test_bedrock_guardrail_is_wired_to_runtime_model() -> None:
    stack = _read("infra/triage_stack.py")
    strands = _read("src/agentcore_runtime/agents/strands_support.py")

    assert "CfnGuardrail(" in stack
    assert "CfnGuardrailVersion(" in stack
    assert "BEDROCK_GUARDRAIL_ID" in stack
    assert "BEDROCK_GUARDRAIL_VERSION" in stack
    assert "guardrail_id" in strands
    assert "guardrail_version" in strands


def test_agentcore_gateway_lambda_uses_gateway_role_identity_policy() -> None:
    stack = _read("infra/triage_stack.py")

    assert "gateway_tool_fn.grant_invoke(gateway_role)" in stack
    assert "AllowAgentCoreGatewayInvokeTools" not in stack


def test_policy_verification_uses_restricted_ssi_account_scope() -> None:
    verify_policy = _read("scripts/verify-policy.sh")

    assert 'client.get_ssi_record("EXC-SYN-10047", "CP-SYN-RESTRICTED-01", "ACCT-SYN-8817")' in verify_policy
