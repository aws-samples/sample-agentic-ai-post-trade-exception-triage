import pytest

from src.agentcore_runtime.tools.gateway_client import (
    AgentCoreGatewayClient,
    GatewayClientError,
    _normalize_gateway_url,
    get_gateway_client,
)


def test_local_gateway_client_is_default_for_in_process_tests(monkeypatch):
    monkeypatch.delenv("AGENTCORE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_IDENTIFIER", raising=False)
    monkeypatch.delenv("AGENTCORE_GATEWAY_REQUIRED", raising=False)
    monkeypatch.setenv("AGENTCORE_GATEWAY_LOCAL_MODE", "1")
    client = get_gateway_client()
    assert client.mode == "local-gateway-handler"
    assert client.get_trade_details("EXC-SYN-10042")["source_id"] == "trade:TRD-SYN-70042"


def test_gateway_required_fails_without_gateway_configuration(monkeypatch):
    monkeypatch.delenv("AGENTCORE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_IDENTIFIER", raising=False)
    monkeypatch.delenv("AGENTCORE_GATEWAY_LOCAL_MODE", raising=False)
    monkeypatch.setenv("AGENTCORE_GATEWAY_REQUIRED", "1")
    with pytest.raises(GatewayClientError, match="Gateway is required"):
        get_gateway_client()


def test_agentcore_gateway_client_builds_prefixed_tool_names(monkeypatch):
    monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://gateway.example.com/mcp")
    monkeypatch.setenv("ALLOW_CUSTOM_GATEWAY_URL", "1")
    client = AgentCoreGatewayClient(target_name="post-trade-synthetic-evidence")
    assert client._mcp_tool_name("get_settlement_status") == "post-trade-synthetic-evidence___get_settlement_status"


def test_gateway_url_normalization_requires_https():
    with pytest.raises(GatewayClientError, match="HTTPS URL"):
        _normalize_gateway_url("file:///tmp/local")
    with pytest.raises(GatewayClientError, match="HTTPS URL"):
        _normalize_gateway_url("http://gateway.example.com/mcp")
    with pytest.raises(GatewayClientError, match="HTTPS URL"):
        _normalize_gateway_url("https:///mcp")


def test_gateway_url_normalization_requires_agentcore_host_for_deployed_mode(monkeypatch):
    monkeypatch.delenv("ALLOW_CUSTOM_GATEWAY_URL", raising=False)
    with pytest.raises(GatewayClientError, match="AgentCore Gateway endpoint"):
        _normalize_gateway_url("https://gateway.example.com/mcp", "us-east-1")


def test_gateway_url_normalization_appends_mcp_path(monkeypatch):
    monkeypatch.setenv("ALLOW_CUSTOM_GATEWAY_URL", "1")
    assert _normalize_gateway_url("https://gateway.example.com") == "https://gateway.example.com/mcp"
    assert _normalize_gateway_url("https://gateway.example.com/mcp") == "https://gateway.example.com/mcp"


def test_agentcore_gateway_client_extracts_lambda_target_content(monkeypatch):
    monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://gateway.example.com/mcp")
    monkeypatch.setenv("ALLOW_CUSTOM_GATEWAY_URL", "1")
    client = AgentCoreGatewayClient()
    monkeypatch.setattr(
        client,
        "_post_json",
        lambda _payload: {
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": '{"toolName":"get_playbook","status":"SUCCESS","content":{"playbook_id":"PB-SSI-001"}}',
                    }
                ]
            },
        },
    )
    assert client.get_playbook("EXC-SYN-10042", "PB-SSI-001") == {"playbook_id": "PB-SSI-001"}


def test_agentcore_gateway_client_normalizes_single_prior_case(monkeypatch):
    monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://gateway.example.com/mcp")
    monkeypatch.setenv("ALLOW_CUSTOM_GATEWAY_URL", "1")
    client = AgentCoreGatewayClient()
    monkeypatch.setattr(
        client,
        "_post_json",
        lambda _payload: {
            "jsonrpc": "2.0",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            '{"toolName":"search_prior_cases","status":"SUCCESS",'
                            '"content":{"case_id":"CASE-SYN-7781","source_id":"prior_case:CASE-SYN-7781"}}'
                        ),
                    }
                ]
            },
        },
    )
    assert client.search_prior_cases("EXC-SYN-10042", "MISSING_OR_STALE_SSI") == [
        {"case_id": "CASE-SYN-7781", "source_id": "prior_case:CASE-SYN-7781"}
    ]


def test_agentcore_gateway_client_treats_mcp_tool_error_as_denial(monkeypatch):
    monkeypatch.setenv("AGENTCORE_GATEWAY_URL", "https://gateway.example.com/mcp")
    monkeypatch.setenv("ALLOW_CUSTOM_GATEWAY_URL", "1")
    client = AgentCoreGatewayClient(target_name="post-trade-synthetic-evidence")

    def fake_post(payload):
        return {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "DENY: restricted counterparty"}],
            },
        }

    monkeypatch.setattr(client, "_post_json", fake_post)
    with pytest.raises(GatewayClientError, match="restricted counterparty"):
        client.get_ssi_record("EXC-SYN-10047", "CP-SYN-RESTRICTED-01", "ACCT-SYN-8820")
