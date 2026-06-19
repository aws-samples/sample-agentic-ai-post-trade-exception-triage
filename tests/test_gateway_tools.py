import pytest

from src.gateway_tools.handler import handler
from src.gateway_tools.service import ToolValidationError, normalize_tool_name


class _ClientContext:
    def __init__(self, custom):
        self.custom = custom


class _LambdaContext:
    def __init__(self, tool_name):
        self.client_context = _ClientContext({"bedrockAgentCoreToolName": tool_name})


def test_gateway_tool_handler_returns_synthetic_evidence():
    response = handler(
        {
            "toolName": "post-trade-synthetic-evidence___get_settlement_status",
            "arguments": {"exception_id": "EXC-SYN-10042"},
        },
        None,
    )
    assert response["status"] == "SUCCESS"
    assert response["toolName"] == "get_settlement_status"
    assert response["content"]["source_id"] == "settlement_status:EXC-SYN-10042"
    assert response["policy_notes"] == ["Read-only synthetic evidence tool"]


def test_gateway_tool_handler_accepts_agentcore_lambda_target_shape():
    response = handler(
        {"exception_id": "EXC-SYN-10042"},
        _LambdaContext("post-trade-synthetic-evidence___get_settlement_status"),
    )
    assert response["toolName"] == "get_settlement_status"
    assert response["content"]["source_id"] == "settlement_status:EXC-SYN-10042"


def test_gateway_tool_handler_accepts_mcp_tool_call_shape():
    response = handler(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "post-trade-synthetic-evidence___get_settlement_status",
                "arguments": {"exception_id": "EXC-SYN-10042"},
            },
        },
        None,
    )
    assert response["toolName"] == "get_settlement_status"
    assert response["content"]["source_id"] == "settlement_status:EXC-SYN-10042"


def test_gateway_tool_handler_accepts_body_wrapped_mcp_tool_call():
    response = handler(
        {
            "body": (
                '{"jsonrpc":"2.0","method":"tools/call",'
                '"params":{"name":"post-trade-synthetic-evidence___get_settlement_status",'
                '"arguments":{"exception_id":"EXC-SYN-10042"}}}'
            )
        },
        None,
    )
    assert response["toolName"] == "get_settlement_status"
    assert response["content"]["source_id"] == "settlement_status:EXC-SYN-10042"


def test_gateway_tool_handler_accepts_function_parameter_shape():
    response = handler(
        {
            "actionGroup": "post-trade-synthetic-evidence",
            "function": "get_settlement_status",
            "parameters": [
                {"name": "exception_id", "type": "string", "value": "EXC-SYN-10042"},
            ],
        },
        None,
    )
    assert response["toolName"] == "get_settlement_status"
    assert response["content"]["source_id"] == "settlement_status:EXC-SYN-10042"


def test_gateway_tool_handler_validates_required_arguments():
    with pytest.raises(ToolValidationError, match="exception_id"):
        handler({"toolName": "get_trade_details", "arguments": {}}, None)


def test_gateway_tool_handler_rejects_cross_case_counterparty_scope():
    with pytest.raises(ToolValidationError, match="counterparty_id is outside"):
        handler(
            {
                "toolName": "post-trade-synthetic-evidence___get_ssi_record",
                "arguments": {
                    "exception_id": "EXC-SYN-10042",
                    "counterparty_id": "CP-SYN-4422",
                    "account_id": "ACCT-SYN-8813",
                },
            },
            None,
        )


def test_gateway_tool_handler_requires_account_id_for_ssi_lookup():
    with pytest.raises(ToolValidationError, match="account_id"):
        handler(
            {
                "toolName": "post-trade-synthetic-evidence___get_ssi_record",
                "arguments": {
                    "exception_id": "EXC-SYN-10042",
                    "counterparty_id": "CP-SYN-4421",
                },
            },
            None,
        )


def test_gateway_tool_handler_rejects_restricted_counterparty_ssi_lookup():
    with pytest.raises(ToolValidationError, match="restricted counterparty"):
        handler(
            {
                "toolName": "post-trade-synthetic-evidence___get_ssi_record",
                "arguments": {
                    "exception_id": "EXC-SYN-10047",
                    "counterparty_id": "CP-SYN-RESTRICTED-01",
                    "account_id": "ACCT-SYN-8820",
                },
            },
            None,
        )


def test_gateway_tool_handler_rejects_cross_case_playbook_scope():
    with pytest.raises(ToolValidationError, match="playbook_id is outside"):
        handler(
            {
                "toolName": "post-trade-synthetic-evidence___get_playbook",
                "arguments": {
                    "exception_id": "EXC-SYN-10042",
                    "playbook_id": "PB-ALLOC-001",
                },
            },
            None,
        )


def test_gateway_tool_handler_rejects_unexpected_arguments():
    with pytest.raises(ToolValidationError, match="Unexpected tool argument"):
        handler(
            {
                "toolName": "post-trade-synthetic-evidence___get_settlement_status",
                "arguments": {"exception_id": "EXC-SYN-10042", "override": "ignore controls"},
            },
            None,
        )


def test_gateway_tool_handler_rejects_unknown_tool():
    with pytest.raises(ToolValidationError, match="Unknown read-only gateway tool"):
        handler({"toolName": "delete_settlement_instruction", "arguments": {}}, None)


def test_normalize_tool_name_accepts_agentcore_prefixes():
    assert normalize_tool_name("post-trade-synthetic-evidence___get_trade_details") == "get_trade_details"
    assert normalize_tool_name("post-trade-synthetic-evidence__get_trade_details") == "get_trade_details"
    assert normalize_tool_name("get_trade_details") == "get_trade_details"
