from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from src.common.data_store import get_data_store
from src.common import config
from src.common.triage_rules import ROOT_CAUSE_TO_PLAYBOOK, infer_root_cause


READ_ONLY_TOOLS = [
    "get_trade_details",
    "get_settlement_status",
    "get_allocation_status",
    "get_ssi_record",
    "search_prior_cases",
    "get_playbook",
]

_ID_PATTERNS = {
    "exception_id": re.compile(r"^EXC-SYN-[0-9]{5}$"),
    "counterparty_id": re.compile(r"^CP-SYN-[A-Z0-9-]{1,32}$"),
    "account_id": re.compile(r"^ACCT-SYN-[0-9]{4}$"),
    "playbook_id": re.compile(r"^PB-[A-Z]+-[0-9]{3}$"),
}


class ToolValidationError(ValueError):
    """Raised when a Gateway tool request is malformed."""


class SyntheticEvidenceToolService:
    """Read-only synthetic evidence tools exposed through AgentCore Gateway.

    This class is intentionally owned by the Gateway tool package, not by the
    Runtime agents. In deployed mode, the Runtime calls AgentCore Gateway over
    MCP; only this Lambda-backed target reads the synthetic data store.
    """

    def __init__(self) -> None:
        self.store = get_data_store()

    def call_tool(self, raw_tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
        tool_name = normalize_tool_name(raw_tool_name)
        if tool_name == "get_trade_details":
            self._reject_unexpected(arguments, {"exception_id"})
            exception_id = self._case_scoped_exception_id(arguments)
            return self.store.trade_details_by_exception(exception_id)
        if tool_name == "get_settlement_status":
            self._reject_unexpected(arguments, {"exception_id"})
            exception_id = self._case_scoped_exception_id(arguments)
            return self.store.settlement_status_by_exception(exception_id)
        if tool_name == "get_allocation_status":
            self._reject_unexpected(arguments, {"exception_id"})
            exception_id = self._case_scoped_exception_id(arguments)
            return self.store.allocation_by_exception(exception_id)
        if tool_name == "get_ssi_record":
            self._reject_unexpected(arguments, {"exception_id", "counterparty_id", "account_id"})
            exception_id = self._case_scoped_exception_id(arguments)
            case = self._case_for_exception(exception_id)
            counterparty_id = self._string_arg(arguments, "counterparty_id", pattern=_ID_PATTERNS["counterparty_id"])
            if counterparty_id.startswith(config.RESTRICTED_COUNTERPARTY_PREFIX):
                raise ToolValidationError("restricted counterparty SSI access is denied")
            account_id = self._string_arg(arguments, "account_id", pattern=_ID_PATTERNS["account_id"])
            self._assert_case_counterparty(case, counterparty_id, account_id)
            record = self.store.ssi_by_counterparty(counterparty_id)
            if record and record.get("account_id") != account_id:
                return None
            return record
        if tool_name == "search_prior_cases":
            self._reject_unexpected(arguments, {"exception_id", "root_cause_category"})
            exception_id = self._case_scoped_exception_id(arguments)
            case = self._case_for_exception(exception_id)
            root_cause = self._root_cause_arg(arguments)
            self._assert_case_root_cause(case, root_cause)
            return self.store.prior_cases_by_root_cause(root_cause)
        if tool_name == "get_playbook":
            self._reject_unexpected(arguments, {"exception_id", "playbook_id"})
            exception_id = self._case_scoped_exception_id(arguments)
            case = self._case_for_exception(exception_id)
            playbook_id = self._string_arg(arguments, "playbook_id", pattern=_ID_PATTERNS["playbook_id"])
            self._assert_case_playbook(case, playbook_id)
            return self.store.playbook_by_id(playbook_id)
        raise ToolValidationError(f"Unknown read-only gateway tool {raw_tool_name}")

    def _case_scoped_exception_id(self, arguments: dict[str, Any]) -> str:
        exception_id = self._string_arg(arguments, "exception_id", pattern=_ID_PATTERNS["exception_id"])
        self._case_for_exception(exception_id)
        return exception_id

    def _case_for_exception(self, exception_id: str) -> dict[str, Any]:
        case = self.store.exception_by_id(exception_id)
        if not case:
            raise ToolValidationError(f"Unknown exception_id outside synthetic case scope: {exception_id}")
        return case

    def _assert_case_counterparty(
        self,
        case: dict[str, Any],
        counterparty_id: str,
        account_id: str | None,
    ) -> None:
        if case.get("counterparty_id") != counterparty_id:
            raise ToolValidationError("counterparty_id is outside the active exception case scope")
        if account_id and case.get("account_id") != account_id:
            raise ToolValidationError("account_id is outside the active exception case scope")

    def _assert_case_root_cause(self, case: dict[str, Any], root_cause: str) -> None:
        expected = infer_root_cause(case)
        if root_cause != expected:
            raise ToolValidationError("root_cause_category is outside the active exception case scope")

    def _assert_case_playbook(self, case: dict[str, Any], playbook_id: str) -> None:
        expected = ROOT_CAUSE_TO_PLAYBOOK[infer_root_cause(case)]
        if playbook_id != expected:
            raise ToolValidationError("playbook_id is outside the active exception case scope")

    def _root_cause_arg(self, arguments: dict[str, Any]) -> str:
        value = self._string_arg(arguments, "root_cause_category")
        if value not in ROOT_CAUSE_TO_PLAYBOOK:
            raise ToolValidationError("root_cause_category is not an approved synthetic root cause")
        return value

    def _string_arg(self, arguments: dict[str, Any], field: str, pattern: re.Pattern[str] | None = None) -> str:
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ToolValidationError(f"Missing required tool argument: {field}")
        value = value.strip()
        if len(value) > 96:
            raise ToolValidationError(f"Tool argument {field} is too long")
        if pattern and not pattern.fullmatch(value):
            raise ToolValidationError(f"Tool argument {field} is not in the expected synthetic ID format")
        return value

    def _optional_string_arg(
        self,
        arguments: dict[str, Any],
        field: str,
        pattern: re.Pattern[str] | None = None,
    ) -> str | None:
        value = arguments.get(field)
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise ToolValidationError(f"Tool argument {field} must be a string")
        value = value.strip()
        if len(value) > 96:
            raise ToolValidationError(f"Tool argument {field} is too long")
        if pattern and not pattern.fullmatch(value):
            raise ToolValidationError(f"Tool argument {field} is not in the expected synthetic ID format")
        return value

    def _reject_unexpected(self, arguments: dict[str, Any], allowed: set[str]) -> None:
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise ToolValidationError(f"Unexpected tool argument(s): {', '.join(unexpected)}")


def normalize_tool_name(tool_name: str) -> str:
    """Return the bare tool name from AgentCore's target-prefixed MCP name."""

    if not tool_name:
        raise ToolValidationError("tool name is required")
    for separator in ("___", "__"):
        if separator in tool_name:
            candidate = tool_name.rsplit(separator, 1)[-1]
            if candidate in READ_ONLY_TOOLS:
                return candidate
    return tool_name


def gateway_tool_response(tool_name: str, result: Any) -> dict[str, Any]:
    return {
        "toolName": normalize_tool_name(tool_name),
        "status": "SUCCESS",
        "content": result,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "policy_notes": ["Read-only synthetic evidence tool"],
    }
