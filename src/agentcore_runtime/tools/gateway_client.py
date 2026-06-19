from __future__ import annotations

import json
import os
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session

from src.common.config import AWS_REGION
from src.gateway_tools.handler import handler as local_gateway_handler
from src.gateway_tools.service import READ_ONLY_TOOLS, normalize_tool_name


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler)


class GatewayClientError(RuntimeError):
    """Raised when AgentCore Gateway cannot return a usable tool result."""


class LocalGatewayToolClient:
    """Local-only adapter that exercises the same Gateway Lambda handler.

    This adapter is used by unit tests and in-process evaluation when no
    deployed Gateway URL is available. Deployed Runtime executions use
    AgentCoreGatewayClient and cannot read the synthetic data store directly.
    """

    mode = "local-gateway-handler"

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        response = local_gateway_handler(
            {"toolName": normalize_tool_name(name), "arguments": arguments},
            None,
        )
        return response.get("content")

    def get_trade_details(self, exception_id: str) -> dict[str, Any] | None:
        return self.call_tool("get_trade_details", {"exception_id": exception_id})

    def get_settlement_status(self, exception_id: str) -> dict[str, Any] | None:
        return self.call_tool("get_settlement_status", {"exception_id": exception_id})

    def get_allocation_status(self, exception_id: str) -> dict[str, Any] | None:
        return self.call_tool("get_allocation_status", {"exception_id": exception_id})

    def get_ssi_record(
        self,
        exception_id: str,
        counterparty_id: str,
        account_id: str,
    ) -> dict[str, Any] | None:
        return self.call_tool(
            "get_ssi_record",
            {"exception_id": exception_id, "counterparty_id": counterparty_id, "account_id": account_id},
        )

    def search_prior_cases(self, exception_id: str, root_cause_category: str) -> list[dict[str, Any]]:
        result = self.call_tool(
            "search_prior_cases",
            {"exception_id": exception_id, "root_cause_category": root_cause_category},
        )
        if result is None:
            return []
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            return [result]
        return []

    def get_playbook(self, exception_id: str, playbook_id: str) -> dict[str, Any] | None:
        return self.call_tool("get_playbook", {"exception_id": exception_id, "playbook_id": playbook_id})


class AgentCoreGatewayClient(LocalGatewayToolClient):
    """MCP/JSON-RPC client for AgentCore Gateway tools."""

    mode = "agentcore-gateway-mcp"

    def __init__(
        self,
        gateway_url: str | None = None,
        region: str | None = None,
        target_name: str | None = None,
        tool_separator: str | None = None,
    ) -> None:
        self.region = region or AWS_REGION
        self.gateway_url = _normalize_gateway_url(gateway_url or _gateway_url_from_env(self.region), self.region)
        self.target_name = target_name or os.environ.get("GATEWAY_TARGET_NAME", "post-trade-synthetic-evidence")
        self.tool_separator = tool_separator or os.environ.get("GATEWAY_TOOL_SEPARATOR", "___")
        self.access_token = os.environ.get("AGENTCORE_GATEWAY_ACCESS_TOKEN", "")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        tool_name = self._mcp_tool_name(name)
        payload = {
            "jsonrpc": "2.0",
            "id": f"call-{uuid.uuid4().hex}",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        response = self._post_json(payload)
        if "error" in response:
            raise GatewayClientError(f"AgentCore Gateway denied or failed tool call {tool_name}: {response['error']}")
        result = response.get("result")
        if isinstance(result, dict) and result.get("isError"):
            raise GatewayClientError(f"AgentCore Gateway denied or failed tool call {tool_name}: {_extract_tool_content(response)}")
        return _extract_tool_content(response)

    def _mcp_tool_name(self, name: str) -> str:
        bare_name = normalize_tool_name(name)
        if bare_name not in READ_ONLY_TOOLS:
            raise GatewayClientError(f"Unknown read-only Gateway tool {name}")
        if os.environ.get("GATEWAY_TOOL_PREFIXED", "1") == "0":
            return bare_name
        return f"{self.target_name}{self.tool_separator}{bare_name}"

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        else:
            headers = _sigv4_headers(self.gateway_url, body, headers, self.region)
        request = Request(self.gateway_url, data=body, headers=headers, method="POST")
        try:
            # _normalize_gateway_url restricts calls to HTTPS AgentCore Gateway
            # endpoints before this Request object is created.
            with _NO_REDIRECT_OPENER.open(request, timeout=float(os.environ.get("AGENTCORE_GATEWAY_TIMEOUT_SECONDS", "20"))) as response:  # nosec B310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise GatewayClientError(f"AgentCore Gateway HTTP {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise GatewayClientError(f"AgentCore Gateway request failed: {exc}") from exc


def get_gateway_client() -> LocalGatewayToolClient:
    if os.environ.get("AGENTCORE_GATEWAY_LOCAL_MODE", "0") == "1":
        return LocalGatewayToolClient()
    gateway_url = _gateway_url_from_env(AWS_REGION)
    if gateway_url:
        return AgentCoreGatewayClient()
    if os.environ.get("AGENTCORE_GATEWAY_REQUIRED", "1") == "1":
        raise GatewayClientError("AgentCore Gateway is required but no Gateway URL or identifier is configured")
    return LocalGatewayToolClient()


def _gateway_url_from_env(region: str) -> str:
    explicit = os.environ.get("AGENTCORE_GATEWAY_URL") or os.environ.get("GATEWAY_URL")
    if explicit:
        return explicit
    gateway_identifier = os.environ.get("GATEWAY_IDENTIFIER", "")
    if not gateway_identifier:
        return ""
    return f"https://{gateway_identifier}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"


def _normalize_gateway_url(url: str, region: str | None = None) -> str:
    if not url:
        raise GatewayClientError("AGENTCORE_GATEWAY_URL or GATEWAY_IDENTIFIER is required for deployed Gateway calls")
    trimmed = url.strip()
    parsed = urlparse(trimmed)
    if parsed.scheme != "https" or not parsed.netloc:
        raise GatewayClientError("AgentCore Gateway URL must be an HTTPS URL with a hostname")
    hostname = parsed.hostname or ""
    resolved_region = region or AWS_REGION
    if os.environ.get("ALLOW_CUSTOM_GATEWAY_URL", "0") != "1":
        expected_suffix = f".gateway.bedrock-agentcore.{resolved_region}.amazonaws.com"
        if not hostname.endswith(expected_suffix):
            raise GatewayClientError("AgentCore Gateway URL hostname must be an AgentCore Gateway endpoint")
    return trimmed if trimmed.rstrip("/").endswith("/mcp") else f"{trimmed.rstrip('/')}/mcp"


def _sigv4_headers(url: str, body: bytes, headers: dict[str, str], region: str) -> dict[str, str]:
    session = get_session()
    credentials = session.get_credentials()
    if credentials is None:
        raise GatewayClientError("AWS credentials are required for AWS_IAM AgentCore Gateway invocation")
    frozen = credentials.get_frozen_credentials()
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(frozen, "bedrock-agentcore", region).add_auth(aws_request)
    return dict(aws_request.headers.items())


def _extract_tool_content(response: dict[str, Any]) -> Any:
    result = response.get("result", response)
    if isinstance(result, dict) and "content" in result:
        content = result["content"]
        if isinstance(content, list):
            return _content_list_value(content)
        if isinstance(content, dict) and "content" in content:
            return content["content"]
        return content
    if isinstance(result, dict) and "structuredContent" in result:
        return result["structuredContent"]
    return result


def _content_list_value(content: list[Any]) -> Any:
    values: list[Any] = []
    for item in content:
        if not isinstance(item, dict):
            values.append(item)
            continue
        if "text" in item:
            text = item["text"]
            try:
                values.append(json.loads(text))
            except (TypeError, json.JSONDecodeError):
                values.append(text)
        elif "json" in item:
            values.append(item["json"])
        elif "data" in item:
            values.append(item["data"])
        else:
            values.append(item)
    if len(values) == 1:
        value = values[0]
        if isinstance(value, dict) and "content" in value:
            return value["content"]
        return value
    return values
