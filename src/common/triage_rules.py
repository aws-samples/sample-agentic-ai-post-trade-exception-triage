from __future__ import annotations

from typing import Any

from . import config


ROOT_CAUSE_TO_PLAYBOOK = {
    "MISSING_OR_STALE_SSI": "PB-SSI-001",
    "ALLOCATION_MISMATCH": "PB-ALLOC-001",
    "COUNTERPARTY_PROFILE_MISMATCH": "PB-CP-001",
    "CONFLICTING_SETTLEMENT_STATUS": "PB-SETTLE-001",
    "MISSING_CONFIRMATION": "PB-CONF-001",
    "STALE_REFERENCE_DATA": "PB-REF-001",
    "POLICY_OR_CUTOFF_ESCALATION": "PB-ESC-001",
}

ROOT_CAUSE_TO_QUEUE = {
    "MISSING_OR_STALE_SSI": "SSI_REMEDIATION",
    "ALLOCATION_MISMATCH": "ALLOCATION_OPERATIONS",
    "COUNTERPARTY_PROFILE_MISMATCH": "COUNTERPARTY_OPERATIONS",
    "CONFLICTING_SETTLEMENT_STATUS": "MANUAL_TRIAGE",
    "MISSING_CONFIRMATION": "CONFIRMATION_OPERATIONS",
    "STALE_REFERENCE_DATA": "REFERENCE_DATA_REMEDIATION",
    "POLICY_OR_CUTOFF_ESCALATION": "URGENT_MANUAL_REVIEW",
}


def normalize_exception(raw: dict[str, Any]) -> dict[str, Any]:
    if not raw:
        raise ValueError("exception payload is empty")
    required = [
        "exception_id",
        "exception_type",
        "product",
        "market",
        "desk",
        "counterparty_id",
        "account_id",
        "settlement_date",
    ]
    missing = [field for field in required if not raw.get(field)]
    if missing:
        raise ValueError(f"missing required exception fields: {', '.join(missing)}")
    normalized = dict(raw)
    normalized.setdefault("allowed_tool_scope", [])
    normalized.setdefault("priority", "MEDIUM")
    normalized.setdefault("status", "OPEN")
    normalized.setdefault("minutes_to_cutoff", 240)
    normalized["case_key"] = normalized.get("case_key", normalized["exception_id"])
    return normalized


def scope_and_severity(case: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    eligible = True
    if case["product"] not in config.SUPPORTED_PRODUCTS:
        eligible = False
        reasons.append("Unsupported product")
    if case["market"] not in config.SUPPORTED_MARKETS:
        eligible = False
        reasons.append("Unsupported market")
    if case["exception_type"] not in config.SUPPORTED_EXCEPTION_TYPES:
        eligible = False
        reasons.append("Unsupported exception type")
    if case["counterparty_id"].startswith(config.RESTRICTED_COUNTERPARTY_PREFIX):
        eligible = False
        reasons.append("Restricted counterparty requires escalation")
    minutes_to_cutoff = int(case.get("minutes_to_cutoff", 240))
    if minutes_to_cutoff <= config.NEAR_CUTOFF_MINUTES:
        eligible = False
        reasons.append("Case is too close to settlement cut-off")
    severity = case.get("priority", "MEDIUM")
    if case.get("exception_type") == "SETTLEMENT_STATUS_MISMATCH":
        severity = "HIGH"
    if minutes_to_cutoff <= config.NEAR_CUTOFF_MINUTES:
        severity = "CRITICAL"
    return {
        "eligible": eligible,
        "severity": severity,
        "eligibility_reasons": reasons or ["Eligible for agent-assisted enrichment"],
        "minutes_to_cutoff": minutes_to_cutoff,
    }


def infer_root_cause(case: dict[str, Any], evidence: dict[str, Any] | None = None) -> str:
    if case["counterparty_id"].startswith(config.RESTRICTED_COUNTERPARTY_PREFIX):
        return "POLICY_OR_CUTOFF_ESCALATION"
    if int(case.get("minutes_to_cutoff", 240)) <= config.NEAR_CUTOFF_MINUTES:
        return "POLICY_OR_CUTOFF_ESCALATION"
    exception_type = case["exception_type"]
    if exception_type == "ALLOCATION_MISMATCH":
        return "ALLOCATION_MISMATCH"
    if exception_type == "COUNTERPARTY_PROFILE_MISMATCH":
        return "COUNTERPARTY_PROFILE_MISMATCH"
    if exception_type == "SETTLEMENT_STATUS_MISMATCH":
        return "CONFLICTING_SETTLEMENT_STATUS"
    if exception_type == "MISSING_CONFIRMATION":
        return "MISSING_CONFIRMATION"
    if exception_type == "REFERENCE_DATA_STALE":
        return "STALE_REFERENCE_DATA"
    if evidence:
        ssi = evidence.get("ssi_record") or {}
        if ssi.get("ssi_status") in {"MISSING", "STALE", "ACTIVE_STALE_REFERENCE"}:
            return "MISSING_OR_STALE_SSI"
    return "MISSING_OR_STALE_SSI"


def recommendation_for(case: dict[str, Any], evidence: dict[str, Any], playbook: dict[str, Any]) -> dict[str, Any]:
    root_cause = playbook["root_cause_category"]
    escalation = root_cause in {"POLICY_OR_CUTOFF_ESCALATION", "CONFLICTING_SETTLEMENT_STATUS"}
    confidence = {
        "MISSING_OR_STALE_SSI": 0.88,
        "ALLOCATION_MISMATCH": 0.87,
        "COUNTERPARTY_PROFILE_MISMATCH": 0.84,
        "CONFLICTING_SETTLEMENT_STATUS": 0.63,
        "MISSING_CONFIRMATION": 0.86,
        "STALE_REFERENCE_DATA": 0.85,
        "POLICY_OR_CUTOFF_ESCALATION": 0.99,
    }[root_cause]
    refs = sorted(
        {
            item["source_id"]
            for item in evidence.values()
            if isinstance(item, dict) and item.get("source_id")
        }
    )
    trade = evidence.get("trade_details", {})
    settlement = evidence.get("settlement_status", {})
    ssi = evidence.get("ssi_record", {})
    allocation = evidence.get("allocation_status", {})
    prior = evidence.get("prior_case", {})
    return {
        "exception_id": case["exception_id"],
        "root_cause_category": root_cause,
        "recommended_queue": playbook["queue"],
        "recommended_action": _recommended_action(root_cause, playbook),
        "confidence": confidence,
        "evidence_refs": refs,
        "playbook_id": playbook["playbook_id"],
        "human_approval_required": True,
        "policy_notes": ["Read-only tools used", "No write action requested"],
        "escalation_reason": "Deterministic escalation gate applies" if escalation else None,
        "analyst_summary": _analyst_summary(root_cause, case, trade, settlement, ssi),
        "decision_rationale": _decision_rationale(root_cause, settlement, ssi, allocation, prior),
        "key_evidence": _key_evidence(trade, settlement, ssi, allocation, prior, playbook),
        "recommended_next_steps": _recommended_next_steps(root_cause),
        "open_questions": _open_questions(root_cause),
        "risk_flags": _risk_flags(root_cause, case, settlement, ssi),
        "suggested_sla_minutes": _suggested_sla_minutes(root_cause, case),
    }


def _recommended_action(root_cause: str, playbook: dict[str, Any]) -> str:
    return {
        "MISSING_OR_STALE_SSI": (
            "Validate the account-level SSI gap with the counterparty/custodian desk, attach the evidence package, "
            "and send the case to SSI remediation for analyst-approved instruction repair."
        ),
        "ALLOCATION_MISMATCH": (
            "Compare block, allocation, and settlement quantities; identify the mismatched allocation leg; "
            "and route to allocation operations with the exact quantity break called out."
        ),
        "COUNTERPARTY_PROFILE_MISMATCH": (
            "Reconcile legal entity, account, and settlement profile mappings before release; route to "
            "counterparty operations with the conflicting identifiers attached."
        ),
        "CONFLICTING_SETTLEMENT_STATUS": (
            "Do not route automatically. Compare source freshness and escalate to manual triage because the "
            "settlement platform and depository status disagree."
        ),
        "MISSING_CONFIRMATION": (
            "Chase the missing confirmation/affirmation, verify allocation readiness, and route to confirmation "
            "operations with the blocking status and trade identifiers attached."
        ),
        "STALE_REFERENCE_DATA": (
            "Refresh the stale security/counterparty reference-data mapping before settlement release, then verify "
            "that the depository match status clears after the approved update."
        ),
        "POLICY_OR_CUTOFF_ESCALATION": (
            "Escalate immediately to urgent manual review; do not rely on agent-assisted enrichment for routing."
        ),
    }.get(root_cause, playbook["approved_actions"][0] + "; route for human analyst review.")


def _analyst_summary(
    root_cause: str,
    case: dict[str, Any],
    trade: dict[str, Any],
    settlement: dict[str, Any],
    ssi: dict[str, Any],
) -> str:
    trade_id = trade.get("trade_id", case.get("trade_id", "the trade"))
    security = trade.get("security_id", case.get("security_id", "the security"))
    quantity = trade.get("quantity")
    status = settlement.get("settlement_status", "UNKNOWN")
    reason = settlement.get("reason", "No settlement reason supplied")
    if root_cause == "STALE_REFERENCE_DATA":
        return (
            f"{trade_id} for {quantity} units of {security} is blocked by {status}. "
            f"The settlement reason is '{reason}', and the SSI/reference record was last verified "
            f"{ssi.get('last_verified', 'unknown')}."
        )
    if root_cause == "ALLOCATION_MISMATCH":
        return (
            f"{trade_id} is blocked by an allocation break. Settlement reports {status}; allocation evidence "
            f"should be compared against the traded quantity {quantity} before the case is released."
        )
    if root_cause == "MISSING_OR_STALE_SSI":
        return (
            f"{trade_id} is unmatched because settlement instructions are missing or stale for "
            f"{case.get('counterparty_id')}/{case.get('account_id')}."
        )
    if root_cause == "MISSING_CONFIRMATION":
        return f"{trade_id} is pending confirmation; settlement cannot progress until affirmation evidence is resolved."
    if root_cause == "COUNTERPARTY_PROFILE_MISMATCH":
        return f"{trade_id} has a counterparty profile mismatch that must be reconciled against account mapping evidence."
    if root_cause == "CONFLICTING_SETTLEMENT_STATUS":
        return f"{trade_id} has conflicting settlement evidence: {reason}."
    return f"{case['exception_id']} requires urgent manual review under policy or settlement cut-off controls."


def _decision_rationale(
    root_cause: str,
    settlement: dict[str, Any],
    ssi: dict[str, Any],
    allocation: dict[str, Any],
    prior: dict[str, Any],
) -> str:
    fragments = []
    if settlement.get("settlement_status"):
        fragments.append(f"settlement status is {settlement['settlement_status']}")
    if settlement.get("depository_status"):
        fragments.append(f"depository status is {settlement['depository_status']}")
    if ssi.get("ssi_status"):
        fragments.append(f"SSI/reference status is {ssi['ssi_status']}")
    if allocation.get("allocation_status"):
        fragments.append(f"allocation status is {allocation['allocation_status']}")
    if prior.get("case_id"):
        fragments.append(f"similar prior case {prior['case_id']} exists")
    basis = "; ".join(fragments) if fragments else "available synthetic evidence supports the selected playbook"
    if root_cause == "STALE_REFERENCE_DATA":
        return f"Reference-data remediation is preferred because {basis}; the record should be refreshed before release."
    if root_cause == "CONFLICTING_SETTLEMENT_STATUS":
        return f"Manual triage is required because {basis}; conflicting source states should not be resolved by the agent."
    if root_cause == "POLICY_OR_CUTOFF_ESCALATION":
        return "Policy and cut-off controls take precedence over agent-assisted routing."
    return f"The selected queue matches the playbook because {basis}."


def _key_evidence(
    trade: dict[str, Any],
    settlement: dict[str, Any],
    ssi: dict[str, Any],
    allocation: dict[str, Any],
    prior: dict[str, Any],
    playbook: dict[str, Any],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if trade:
        rows.append(
            {
                "label": "Trade",
                "value": f"{trade.get('trade_id')} {trade.get('side')} {trade.get('quantity')} {trade.get('security_id')}",
                "source_ref": trade.get("source_id", ""),
            }
        )
    if settlement:
        rows.append(
            {
                "label": "Settlement status",
                "value": f"{settlement.get('settlement_status')} / {settlement.get('depository_status')}: {settlement.get('reason')}",
                "source_ref": settlement.get("source_id", ""),
            }
        )
    if ssi:
        rows.append(
            {
                "label": "SSI/reference record",
                "value": f"{ssi.get('ssi_status')} at {ssi.get('custodian')} last verified {ssi.get('last_verified')}",
                "source_ref": ssi.get("source_id", ""),
            }
        )
    if allocation:
        rows.append(
            {
                "label": "Allocation",
                "value": f"{allocation.get('allocation_status')}: {allocation.get('reason')}",
                "source_ref": allocation.get("source_id", ""),
            }
        )
    if prior:
        rows.append(
            {
                "label": "Prior case",
                "value": f"{prior.get('case_id')}: {prior.get('resolution_summary', prior.get('root_cause_category'))}",
                "source_ref": prior.get("source_id", ""),
            }
        )
    rows.append(
        {
            "label": "Playbook",
            "value": f"{playbook.get('playbook_id')} - {playbook.get('title')}",
            "source_ref": playbook.get("source_id", ""),
        }
    )
    return rows


def _recommended_next_steps(root_cause: str) -> list[str]:
    return {
        "MISSING_OR_STALE_SSI": [
            "Confirm whether the receiving account has an approved SSI on file.",
            "Ask the counterparty/custodian desk to validate the latest instruction.",
            "Keep the case open until an analyst approves the instruction update.",
        ],
        "ALLOCATION_MISMATCH": [
            "Compare block quantity, allocated quantity, and settlement quantity side by side.",
            "Identify the allocation leg that created the break.",
            "Route the enriched package to allocation operations for correction approval.",
        ],
        "COUNTERPARTY_PROFILE_MISMATCH": [
            "Compare legal entity and account identifiers across trade and settlement evidence.",
            "Validate whether the counterparty profile changed after trade capture.",
            "Route to counterparty operations with mismatched identifiers highlighted.",
        ],
        "CONFLICTING_SETTLEMENT_STATUS": [
            "Compare source timestamps and system ownership for both settlement states.",
            "Escalate before taking any routing or repair action.",
            "Record the authoritative source decision in the case notes.",
        ],
        "MISSING_CONFIRMATION": [
            "Check whether confirmation was sent, received, and affirmed.",
            "Attach allocation readiness evidence to the confirmation chase.",
            "Route to confirmation operations for analyst follow-up.",
        ],
        "STALE_REFERENCE_DATA": [
            "Validate the latest approved security and counterparty reference-data attributes.",
            "Refresh the stale mapping through the governed reference-data workflow.",
            "Recheck settlement/depository match status after the approved update.",
        ],
        "POLICY_OR_CUTOFF_ESCALATION": [
            "Escalate immediately to urgent manual review.",
            "Record the policy or cut-off reason.",
            "Avoid agent-assisted enrichment until the control owner approves next steps.",
        ],
    }[root_cause]


def _open_questions(root_cause: str) -> list[str]:
    return {
        "MISSING_OR_STALE_SSI": [
            "Is there an approved SSI that has not propagated to settlement?",
            "Did the counterparty or account mapping change after trade capture?",
        ],
        "ALLOCATION_MISMATCH": [
            "Which allocation leg differs from the block trade?",
            "Was there a late allocation amendment not reflected downstream?",
        ],
        "COUNTERPARTY_PROFILE_MISMATCH": [
            "Which system owns the current legal entity mapping?",
            "Is the mismatch caused by stale profile data or an incorrect trade capture value?",
        ],
        "CONFLICTING_SETTLEMENT_STATUS": [
            "Which source is authoritative for this market and settlement window?",
            "Is one of the statuses stale or delayed?",
        ],
        "MISSING_CONFIRMATION": [
            "Was the confirmation sent to the expected channel?",
            "Is the counterparty awaiting allocation correction before affirming?",
        ],
        "STALE_REFERENCE_DATA": [
            "Which reference-data attribute changed after trade capture?",
            "Has the approved source-of-record update propagated to settlement and depository matching?",
        ],
        "POLICY_OR_CUTOFF_ESCALATION": [
            "Has the control owner approved any exception to normal routing?",
            "Is settlement cut-off risk already customer-impacting?",
        ],
    }[root_cause]


def _risk_flags(root_cause: str, case: dict[str, Any], settlement: dict[str, Any], ssi: dict[str, Any]) -> list[str]:
    flags = []
    if settlement.get("depository_status") == "UNMATCHED":
        flags.append("Depository unmatched")
    if int(case.get("minutes_to_cutoff", 240)) <= config.NEAR_CUTOFF_MINUTES:
        flags.append("Near settlement cut-off")
    if case.get("priority") in {"HIGH", "CRITICAL"}:
        flags.append(f"{case['priority']} priority")
    if ssi.get("ssi_status") in {"MISSING", "STALE", "ACTIVE_STALE_REFERENCE"}:
        flags.append(f"SSI/reference status: {ssi['ssi_status']}")
    if root_cause == "CONFLICTING_SETTLEMENT_STATUS":
        flags.append("Conflicting source evidence")
    if root_cause == "POLICY_OR_CUTOFF_ESCALATION":
        flags.append("Policy-controlled escalation")
    return flags or ["No additional risk flags from synthetic evidence"]


def _suggested_sla_minutes(root_cause: str, case: dict[str, Any]) -> int:
    if root_cause == "POLICY_OR_CUTOFF_ESCALATION":
        return 15
    if int(case.get("minutes_to_cutoff", 240)) <= 60:
        return 15
    if case.get("priority") == "HIGH":
        return 30
    if root_cause == "CONFLICTING_SETTLEMENT_STATUS":
        return 20
    return 60


def validate_recommendation(case: dict[str, Any], recommendation: dict[str, Any]) -> dict[str, Any]:
    required = {
        "exception_id",
        "root_cause_category",
        "recommended_queue",
        "recommended_action",
        "confidence",
        "evidence_refs",
        "playbook_id",
        "human_approval_required",
        "policy_notes",
    }
    missing = sorted(required - recommendation.keys())
    errors = [f"Missing field: {field}" for field in missing]
    if recommendation.get("exception_id") != case["exception_id"]:
        errors.append("Recommendation exception_id does not match case")
    if not isinstance(recommendation.get("evidence_refs"), list) or not recommendation.get("evidence_refs"):
        errors.append("Recommendation must include evidence_refs")
    elif not all(isinstance(item, str) and item for item in recommendation["evidence_refs"]):
        errors.append("Recommendation evidence_refs must be non-empty strings")
    root_cause = recommendation.get("root_cause_category")
    if root_cause not in ROOT_CAUSE_TO_PLAYBOOK:
        errors.append("Recommendation root_cause_category is not approved")
    else:
        if root_cause != infer_root_cause(case):
            errors.append("Recommendation root_cause_category does not match deterministic case classification")
        if recommendation.get("playbook_id") != ROOT_CAUSE_TO_PLAYBOOK[root_cause]:
            errors.append("Recommendation playbook_id does not match approved root cause mapping")
        if recommendation.get("recommended_queue") != ROOT_CAUSE_TO_QUEUE[root_cause]:
            errors.append("Recommendation queue does not match approved root cause mapping")
    if recommendation.get("human_approval_required") is not True:
        errors.append("Recommendation must require human approval")
    if not isinstance(recommendation.get("policy_notes"), list) or not recommendation.get("policy_notes"):
        errors.append("Recommendation must include policy_notes")
    try:
        confidence = float(recommendation.get("confidence", 0))
    except (TypeError, ValueError):
        # Model sometimes returns non-numeric confidence (e.g. "MEDIUM"); treat
        # as invalid so downstream gate routes to MANUAL_TRIAGE rather than
        # crashing the evaluator. Correctness property preserved: agents never
        # override validation / routing; a malformed recommendation is refused.
        errors.append("Recommendation confidence must be numeric in [0, 1]")
        confidence = 0.0
    if not 0 <= confidence <= 1:
        errors.append("Recommendation confidence must be numeric in [0, 1]")
    accepted = not errors
    decision = "VALID" if accepted else "INVALID"
    return {"decision": decision, "accepted": accepted, "confidence": confidence, "errors": errors}


def policy_confidence_gate(case: dict[str, Any], recommendation: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    if not validation["accepted"]:
        return {"decision": "MANUAL_TRIAGE", "reason": "; ".join(validation["errors"])}
    if case["counterparty_id"].startswith(config.RESTRICTED_COUNTERPARTY_PREFIX):
        return {"decision": "ESCALATE", "reason": "Restricted counterparty"}
    if int(case.get("minutes_to_cutoff", 240)) <= config.NEAR_CUTOFF_MINUTES:
        return {"decision": "ESCALATE", "reason": "Near settlement cut-off"}
    if recommendation.get("root_cause_category") == "CONFLICTING_SETTLEMENT_STATUS":
        return {"decision": "ESCALATE", "reason": "Conflicting settlement evidence"}
    try:
        rec_confidence = float(recommendation.get("confidence", 0))
    except (TypeError, ValueError):
        # Defense-in-depth: if we ever reach this branch with a non-numeric
        # confidence, route to MANUAL_TRIAGE. validate_recommendation above
        # should have already rejected it.
        rec_confidence = 0.0
    if rec_confidence < config.CONFIDENCE_THRESHOLD:
        return {"decision": "MANUAL_TRIAGE", "reason": "Confidence below threshold"}
    return {"decision": "ROUTE_ENRICHED_CASE", "reason": "Policy and confidence gates met"}
