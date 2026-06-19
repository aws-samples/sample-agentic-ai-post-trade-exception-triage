#!/usr/bin/env python3
import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from infra.triage_stack import AgenticPostTradeTriageStack


def _coerce_bool(value, default: bool) -> bool:
    """Coerce a CDK context value to a bool. CDK context comes through as the
    raw string from -c key=value, or as a Python bool from cdk.json. We accept
    the common truthy/falsy spellings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


app = cdk.App()
response_streaming = _coerce_bool(app.node.try_get_context("responseStreaming"), default=True)
enable_evaluator = _coerce_bool(app.node.try_get_context("enableEvaluator"), default=True)
stack_name = app.node.try_get_context("stackName") or "AgenticPostTradeExceptionTriageStack"

AgenticPostTradeTriageStack(
    app,
    stack_name,
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "us-east-1",
    ),
    response_streaming=response_streaming,
    enable_evaluator=enable_evaluator,
)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
