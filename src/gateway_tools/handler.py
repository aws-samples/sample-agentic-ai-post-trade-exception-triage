from __future__ import annotations

import json
from typing import Any

from src.gateway_tools.service import SyntheticEvidenceToolService, ToolValidationError, gateway_tool_response, normalize_tool_name


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    tool_name, arguments = _extract_tool_request(event, context)
    service = SyntheticEvidenceToolService()
    result = service.call_tool(normalize_tool_name(tool_name), arguments)
    return gateway_tool_response(tool_name, result)


def _extract_tool_request(event: dict[str, Any], context: Any = None) -> tuple[str | None, dict[str, Any]]:
    payload = _parse_body(event)
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    tool_use = payload.get("toolUse") if isinstance(payload.get("toolUse"), dict) else {}

    tool_name = (
        payload.get("toolName")
        or payload.get("name")
        or payload.get("tool")
        or payload.get("function")
        or payload.get("operationId")
        or params.get("toolName")
        or params.get("name")
        or params.get("tool")
        or params.get("function")
        or tool_use.get("toolName")
        or tool_use.get("name")
        or tool_use.get("tool")
        or _tool_name_from_context(context)
    )
    raw_arguments = _explicit_arguments(payload, params, tool_use)
    if not tool_name:
        print("Gateway tool event missing tool name:", json.dumps(event, default=str)[:4000])
    arguments = _coerce_arguments(raw_arguments if raw_arguments is not None else _argument_map_from_payload(payload))
    return tool_name, arguments


def _explicit_arguments(payload: dict[str, Any], params: dict[str, Any], tool_use: dict[str, Any]) -> Any:
    for source in (payload, params, tool_use):
        for key in ("arguments", "input", "parameters"):
            if key in source:
                return source[key]
    return None


def _argument_map_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    metadata_keys = {
        "actionGroup",
        "body",
        "function",
        "input",
        "jsonrpc",
        "method",
        "name",
        "operationId",
        "parameters",
        "params",
        "tool",
        "toolName",
        "toolUse",
    }
    return {key: value for key, value in payload.items() if key not in metadata_keys}


def _coerce_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolValidationError(f"tool arguments must be valid JSON: {exc}") from exc
    if isinstance(arguments, list):
        return {
            item["name"]: item.get("value")
            for item in arguments
            if isinstance(item, dict) and item.get("name")
        }
    if not isinstance(arguments, dict):
        raise ToolValidationError("tool arguments must be an object")
    return arguments


def _tool_name_from_context(context: Any) -> str | None:
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if isinstance(client_context, dict):
        custom = client_context.get("custom")
    if isinstance(custom, dict):
        return custom.get("bedrockAgentCoreToolName")
    return None


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    if isinstance(body, str) and body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ToolValidationError(f"body must be valid JSON: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
        raise ToolValidationError("body must decode to a JSON object")
    if isinstance(body, dict):
        return body
    return event
