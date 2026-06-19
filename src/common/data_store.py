from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import config


DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _load_json(name: str) -> list[dict[str, Any]]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


class LocalDataStore:
    def __init__(self) -> None:
        self.exceptions = _load_json("exceptions.json")
        self.trade_details = _load_json("trade_details.json")
        self.settlement_status = _load_json("settlement_status.json")
        self.allocations = _load_json("allocations.json")
        self.ssi_records = _load_json("ssi_records.json")
        self.prior_cases = _load_json("prior_cases.json")
        self.playbooks = _load_json("playbooks.json")
        self.golden_dataset = _load_json("golden_dataset.json")
        self.inbound_break_files = _load_json("inbound_break_files.json")

    def exception_by_case_key(self, case_key: str) -> dict[str, Any] | None:
        return _first(self.exceptions, "case_key", case_key)

    def exception_by_id(self, exception_id: str) -> dict[str, Any] | None:
        return _first(self.exceptions, "exception_id", exception_id)

    def inbound_break_by_case_key(self, case_key: str) -> dict[str, Any] | None:
        return _first(self.inbound_break_files, "case_key", case_key)

    def inbound_break_by_exception(self, exception_id: str) -> dict[str, Any] | None:
        return _first(self.inbound_break_files, "exception_id", exception_id)

    def trade_details_by_exception(self, exception_id: str) -> dict[str, Any] | None:
        return _first(self.trade_details, "exception_id", exception_id)

    def settlement_status_by_exception(self, exception_id: str) -> dict[str, Any] | None:
        return _first(self.settlement_status, "exception_id", exception_id)

    def allocation_by_exception(self, exception_id: str) -> dict[str, Any] | None:
        return _first(self.allocations, "exception_id", exception_id)

    def ssi_by_counterparty(self, counterparty_id: str) -> dict[str, Any] | None:
        return _first(self.ssi_records, "counterparty_id", counterparty_id)

    def prior_cases_by_root_cause(self, root_cause_category: str) -> list[dict[str, Any]]:
        return [item for item in self.prior_cases if item["root_cause_category"] == root_cause_category]

    def playbook_by_id(self, playbook_id: str) -> dict[str, Any] | None:
        return _first(self.playbooks, "playbook_id", playbook_id)

    def playbook_by_root_cause(self, root_cause_category: str) -> dict[str, Any] | None:
        return _first(self.playbooks, "root_cause_category", root_cause_category)


def _first(items: list[dict[str, Any]], key: str, value: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get(key) == value), None)


def table_enabled() -> bool:
    return bool(config.TABLE_NAME and os.environ.get("USE_LOCAL_DATA", "0") != "1")


def decimal_safe(value: Any) -> Any:
    if isinstance(value, list):
        return [decimal_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: decimal_safe(v) for k, v in value.items()}
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def plain_json(value: Any) -> Any:
    if isinstance(value, list):
        return [plain_json(v) for v in value]
    if isinstance(value, dict):
        return {k: plain_json(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    return value


class DynamoDataStore(LocalDataStore):
    def __init__(self) -> None:
        import boto3

        self.table = boto3.resource("dynamodb").Table(config.TABLE_NAME)
        super().__init__()

    def get_item(self, pk: str, sk: str) -> dict[str, Any] | None:
        response = self.table.get_item(Key={"PK": pk, "SK": sk})
        return plain_json(response.get("Item"))

    def exception_by_case_key(self, case_key: str) -> dict[str, Any] | None:
        for item in self.exceptions:
            if item["case_key"] == case_key:
                return self.exception_by_id(item["exception_id"])
        return None

    def exception_by_id(self, exception_id: str) -> dict[str, Any] | None:
        item = self.get_item(f"EXCEPTION#{exception_id}", "METADATA")
        return item.get("payload") if item else None

    def trade_details_by_exception(self, exception_id: str) -> dict[str, Any] | None:
        item = self.get_item(f"EXCEPTION#{exception_id}", "TRADE")
        return item.get("payload") if item else None

    def settlement_status_by_exception(self, exception_id: str) -> dict[str, Any] | None:
        item = self.get_item(f"EXCEPTION#{exception_id}", "SETTLEMENT_STATUS")
        return item.get("payload") if item else None

    def allocation_by_exception(self, exception_id: str) -> dict[str, Any] | None:
        item = self.get_item(f"EXCEPTION#{exception_id}", "ALLOCATION")
        return item.get("payload") if item else None

    def ssi_by_counterparty(self, counterparty_id: str) -> dict[str, Any] | None:
        item = self.get_item(f"COUNTERPARTY#{counterparty_id}", "SSI")
        return item.get("payload") if item else None

    def playbook_by_id(self, playbook_id: str) -> dict[str, Any] | None:
        item = self.get_item(f"PLAYBOOK#{playbook_id}", "METADATA")
        return item.get("payload") if item else None

    def playbook_by_root_cause(self, root_cause_category: str) -> dict[str, Any] | None:
        for item in self.playbooks:
            if item["root_cause_category"] == root_cause_category:
                return self.playbook_by_id(item["playbook_id"])
        return None


def get_data_store() -> LocalDataStore:
    if table_enabled():
        return DynamoDataStore()
    return LocalDataStore()
