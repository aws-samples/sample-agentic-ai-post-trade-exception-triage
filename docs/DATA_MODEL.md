# Data Model

## Principles

All data is synthetic and fictional. Do not use real names, real accounts, real counterparties, real securities, real trade IDs, real customer data, or real operational records.

The data model should be simple enough for customers to understand and rich enough to demonstrate the flow.

## Synthetic Exception Input

Example:

```json
{
  "exception_id": "EXC-10042",
  "case_key": "missing_ssi",
  "exception_type": "SETTLEMENT_INSTRUCTION_MISMATCH",
  "product": "EQUITY",
  "market": "US",
  "desk": "US_CASH_EQUITIES",
  "legal_entity": "SYNTHETIC_BROKER_DEALER_US",
  "counterparty_id": "CP-SYN-4421",
  "account_id": "ACCT-SYN-8812",
  "security_id": "SYNTH-EQ-001",
  "trade_date": "2026-04-23",
  "settlement_date": "2026-04-24",
  "priority": "HIGH",
  "source_system": "synthetic-reconciliation-platform",
  "allowed_tool_scope": ["trade", "settlement", "allocation", "ssi", "playbook", "prior_case"]
}
```

## Inbound Break File Trace

The UI also shows the synthetic record as it arrived from an internal break file before normalization. This trace is intentionally kept separate from the normalized `case` contract so downstream gates and the agent consume clean fields while reviewers can still inspect the original file row.

Example:

```json
{
  "case_key": "missing_ssi",
  "exception_id": "EXC-SYN-10042",
  "source_system": "synthetic-reconciliation-platform",
  "file_name": "settlement_breaks_2026-04-24_1320.csv",
  "record_number": 17,
  "raw_record": {
    "brk_cd": "SSI_MISS",
    "sev": "H",
    "cp": "CP-SYN-4421",
    "acct": "ACCT-SYN-8812",
    "sd": "20260424",
    "dep_stat": "UNMATCHED"
  },
  "field_mapping": [
    {
      "source_field": "brk_cd",
      "target_field": "exception_type",
      "raw_value": "SSI_MISS",
      "normalized_value": "SETTLEMENT_INSTRUCTION_MISMATCH",
      "transform": "Break-code lookup"
    }
  ]
}
```

## Recommendation Output

Example:

```json
{
  "exception_id": "EXC-10042",
  "root_cause_category": "MISSING_OR_STALE_SSI",
  "recommended_queue": "SSI_REMEDIATION",
  "recommended_action": "Review SSI evidence and update settlement instructions before release",
  "confidence": 0.86,
  "evidence_refs": [
    "settlement_status:EXC-10042",
    "ssi_record:CP-SYN-4421",
    "prior_case:CASE-SYN-7781"
  ],
  "playbook_id": "PB-SSI-001",
  "human_approval_required": true,
  "policy_notes": ["Read-only tools used", "No write action requested"],
  "escalation_reason": null,
  "analyst_summary": "TRD-SYN-70042 is unmatched because settlement instructions are missing for CP-SYN-4421/ACCT-SYN-8812.",
  "decision_rationale": "The selected queue matches the playbook because settlement status is PENDING_INSTRUCTION; depository status is UNMATCHED; SSI/reference status is MISSING.",
  "key_evidence": [
    {
      "label": "Settlement status",
      "value": "PENDING_INSTRUCTION / UNMATCHED: Missing standing settlement instruction for receiving account",
      "source_ref": "settlement_status:EXC-SYN-10042"
    }
  ],
  "recommended_next_steps": [
    "Confirm whether the receiving account has an approved SSI on file.",
    "Ask the counterparty/custodian desk to validate the latest instruction."
  ],
  "open_questions": [
    "Is there an approved SSI that has not propagated to settlement?"
  ],
  "risk_flags": ["Depository unmatched", "HIGH priority", "SSI/reference status: MISSING"],
  "suggested_sla_minutes": 30
}
```

## Audit Record

Example:

```json
{
  "audit_id": "AUDIT-EXC-SYN-10042-001",
  "exception_id": "EXC-SYN-10042",
  "execution_arn": "arn:aws:states:us-east-1:111122223333:execution:<generated-state-machine-name>:ui-missing_ssi-1760000000-abc123",
  "agent_session_id": "session-EXC-SYN-10042",
  "bedrock_model_id": "us.anthropic.claude-opus-4-6-v1",
  "evidence_source_ids": ["settlement_status:EXC-SYN-10042", "ssi_record:CP-SYN-4421"],
  "recommendation_evidence_refs": ["settlement_status:EXC-SYN-10042", "ssi_record:CP-SYN-4421"],
  "recommended_queue": "SSI_REMEDIATION",
  "playbook_id": "PB-SSI-001",
  "eligibility_decision": "ELIGIBLE",
  "validation_decision": "VALID",
  "routing_decision": "ROUTED_TO_ANALYST_QUEUE",
  "policy_decisions": [
    {
      "tool": "get_settlement_status",
      "decision": "ALLOW"
    }
  ],
  "created_at": "2026-04-24T14:35:00Z"
}
```

## Required Synthetic Cases

| Case key | Purpose | Expected outcome |
| --- | --- | --- |
| `missing_ssi` | Demonstrates missing or stale standing settlement instruction evidence. | Enriched analyst case routed to SSI remediation. |
| `unmatched_allocation` | Demonstrates allocation mismatch evidence and playbook mapping. | Enriched analyst case routed to allocation operations. |
| `counterparty_mismatch` | Demonstrates counterparty profile mismatch. | Enriched analyst case routed to counterparty operations. |
| `settlement_status_mismatch` | Demonstrates conflicting settlement status evidence. | Escalate or route with low confidence depending on evidence. |
| `missing_confirmation` | Demonstrates missing confirmation or affirmation evidence. | Route to confirmation operations. |
| `restricted_counterparty` | Demonstrates policy-controlled escalation. | Escalate for restricted counterparty review. |
| `near_cutoff_escalation` | Demonstrates settlement cut-off sensitivity. | Escalate to urgent manual review. |
| `stale_reference_data` | Demonstrates stale reference data detection. | Route to reference data remediation. |

## Synthetic Datasets

The implementation should provide source JSON files under `data/` and seed them into DynamoDB and S3.

Required datasets:

- `exceptions.json`
- `trade_details.json`
- `settlement_status.json`
- `allocations.json`
- `ssi_records.json`
- `prior_cases.json`
- `playbooks.json`
- `golden_dataset.json`
- `inbound_break_files.json`

## DynamoDB Design

Use a single-table design unless implementation complexity requires separate tables.

Suggested keys:

- Partition key: `PK`
- Sort key: `SK`
- Entity type attribute: `entity_type`

Suggested item patterns:

- `PK=EXCEPTION#<exception_id>`, `SK=METADATA`
- `PK=EXCEPTION#<exception_id>`, `SK=AUDIT#<timestamp>`
- `PK=COUNTERPARTY#<counterparty_id>`, `SK=SSI`
- `PK=EXCEPTION#<exception_id>`, `SK=SETTLEMENT_STATUS`
- `PK=EXCEPTION#<exception_id>`, `SK=ALLOCATION`
- `PK=PLAYBOOK#<playbook_id>`, `SK=METADATA`
- `PK=CASE_HISTORY#<root_cause_category>`, `SK=<case_id>`
