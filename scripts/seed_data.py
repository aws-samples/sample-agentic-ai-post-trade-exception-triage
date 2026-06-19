from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import boto3


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def decimal_safe(value):
    if isinstance(value, list):
        return [decimal_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: decimal_safe(v) for k, v in value.items()}
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def main() -> None:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    table_name = os.environ["TABLE_NAME"]
    bucket = os.environ["ARTIFACT_BUCKET"]
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    s3 = boto3.client("s3", region_name=region)

    items = []
    for item in load("exceptions.json"):
        items.append({"PK": f"EXCEPTION#{item['exception_id']}", "SK": "METADATA", "entity_type": "EXCEPTION", "payload": item})
    for item in load("trade_details.json"):
        items.append({"PK": f"EXCEPTION#{item['exception_id']}", "SK": "TRADE", "entity_type": "TRADE", "payload": item})
    for item in load("settlement_status.json"):
        items.append({"PK": f"EXCEPTION#{item['exception_id']}", "SK": "SETTLEMENT_STATUS", "entity_type": "SETTLEMENT_STATUS", "payload": item})
    for item in load("allocations.json"):
        items.append({"PK": f"EXCEPTION#{item['exception_id']}", "SK": "ALLOCATION", "entity_type": "ALLOCATION", "payload": item})
    for item in load("ssi_records.json"):
        items.append({"PK": f"COUNTERPARTY#{item['counterparty_id']}", "SK": "SSI", "entity_type": "SSI", "payload": item})
    for item in load("playbooks.json"):
        items.append({"PK": f"PLAYBOOK#{item['playbook_id']}", "SK": "METADATA", "entity_type": "PLAYBOOK", "payload": item})
    for item in load("prior_cases.json"):
        items.append({"PK": f"CASE_HISTORY#{item['root_cause_category']}", "SK": item["case_id"], "entity_type": "PRIOR_CASE", "payload": item})

    with table.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
        for item in items:
            batch.put_item(Item=decimal_safe(item))

    for file_name in [
        "exceptions.json",
        "trade_details.json",
        "settlement_status.json",
        "allocations.json",
        "ssi_records.json",
        "prior_cases.json",
        "playbooks.json",
        "golden_dataset.json",
        "inbound_break_files.json",
    ]:
        s3.upload_file(str(DATA / file_name), bucket, f"data/{file_name}")

    print(f"Seeded {len(items)} DynamoDB items into {table_name}")
    print(f"Uploaded synthetic datasets to s3://{bucket}/data/")


if __name__ == "__main__":
    main()
