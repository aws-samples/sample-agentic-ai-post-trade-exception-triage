import json
import os

os.environ.setdefault("AGENTCORE_GATEWAY_LOCAL_MODE", "1")

from src.agentcore_runtime.triage_workflow import run_triage
from src.agentcore_runtime.agents import recommendation_agent, summary_agent
from src.agentcore_runtime.agents.evidence_agent import run_evidence_stage
from src.agentcore_runtime.agents.playbook_agent import run_playbook_stage
from src.agentcore_runtime.agents.summary_agent import run_summary_stage
from src.common.data_store import get_data_store
from src.common.triage_rules import (
    normalize_exception,
    policy_confidence_gate,
    recommendation_for,
    scope_and_severity,
    validate_recommendation,
)
from src.lambda_tasks.handlers import _summarize_history, normalize_exception_handler


def test_missing_ssi_routes_to_ssi_remediation(monkeypatch):
    monkeypatch.setenv("DISABLE_STRANDS_MODEL_CALL", "1")
    case = normalize_exception(get_data_store().exception_by_case_key("missing_ssi"))
    scope = scope_and_severity(case)
    assert scope["eligible"] is True
    agent = run_triage(case)
    recommendation = agent["recommendation"]
    validation = validate_recommendation(case, recommendation)
    gate = policy_confidence_gate(case, recommendation, validation)
    assert recommendation["root_cause_category"] == "MISSING_OR_STALE_SSI"
    assert recommendation["playbook_id"] == "PB-SSI-001"
    assert recommendation["recommended_queue"] == "SSI_REMEDIATION"
    assert "ssi_record:CP-SYN-4421" in recommendation["evidence_refs"]
    assert validation["accepted"] is True
    assert gate["decision"] == "ROUTE_ENRICHED_CASE"


def test_recommendation_normalizes_model_key_evidence_shape(monkeypatch):
    monkeypatch.setenv("DISABLE_STRANDS_MODEL_CALL", "1")
    case = normalize_exception(get_data_store().exception_by_case_key("missing_ssi"))
    summary = run_summary_stage(case)
    evidence_package = run_evidence_stage(case, summary)
    playbook_match = run_playbook_stage(case, summary, evidence_package)
    deterministic = recommendation_for(case, evidence_package["evidence"], playbook_match["playbook"])

    def malformed_model_output(_prompt, _payload):
        output = dict(deterministic)
        output["key_evidence"] = [
            {
                "evidence": "Trade",
                "signal": "Synthetic trade matched",
                "source": "trade_details:EXC-SYN-10042",
            },
            {
                "evidence": "SSI/reference record",
                "signal": "Missing standing settlement instruction",
                "source": "ssi_record:CP-SYN-4421",
            },
        ]
        return output

    monkeypatch.setattr(recommendation_agent, "run_strands_json", malformed_model_output)
    recommendation = recommendation_agent.run_recommendation_stage(case, evidence_package, playbook_match)

    assert recommendation["key_evidence"][0] == {
        "label": "Trade",
        "value": "Synthetic trade matched",
        "source_ref": "trade_details:EXC-SYN-10042",
    }
    assert recommendation["key_evidence"][1] == {
        "label": "SSI/reference record",
        "value": "Missing standing settlement instruction",
        "source_ref": "ssi_record:CP-SYN-4421",
    }
    assert all(item["label"] and item["value"] and item["source_ref"] for item in recommendation["key_evidence"])


def test_recommendation_falls_back_for_unusable_model_key_evidence(monkeypatch):
    monkeypatch.setenv("DISABLE_STRANDS_MODEL_CALL", "1")
    case = normalize_exception(get_data_store().exception_by_case_key("missing_ssi"))
    summary = run_summary_stage(case)
    evidence_package = run_evidence_stage(case, summary)
    playbook_match = run_playbook_stage(case, summary, evidence_package)
    deterministic = recommendation_for(case, evidence_package["evidence"], playbook_match["playbook"])

    def unusable_model_output(_prompt, _payload):
        output = dict(deterministic)
        output["key_evidence"] = [{"evidence": "Trade"}, {"source": "ssi_record:CP-SYN-4421"}]
        return output

    monkeypatch.setattr(recommendation_agent, "run_strands_json", unusable_model_output)
    recommendation = recommendation_agent.run_recommendation_stage(case, evidence_package, playbook_match)

    assert recommendation["key_evidence"] == deterministic["key_evidence"]
    assert all(item["label"] and item["value"] and item["source_ref"] for item in recommendation["key_evidence"])


def test_normalize_exception_includes_inbound_break_trace():
    result = normalize_exception_handler({"exception": {"case_key": "missing_ssi"}}, None)
    assert result["case"]["exception_id"] == "EXC-SYN-10042"
    trace = result["normalization"]
    source = trace["source_break"]
    assert source["file_name"] == "settlement_breaks_2026-04-24_1320.csv"
    assert source["record_number"] == 17
    assert source["raw_record"]["brk_cd"] == "SSI_MISS"
    assert trace["normalized_contract"]["exception_type"] == "SETTLEMENT_INSTRUCTION_MISMATCH"
    assert any(item["field"] == "minutes_to_cutoff" for item in trace["defaults_applied"])
    assert any(item["source_field"] == "brk_cd" for item in source["field_mapping"])


def test_history_summary_exposes_normalize_output_for_ui():
    normalized = normalize_exception_handler({"exception": {"case_key": "missing_ssi"}}, None)
    events = [
        {
            "type": "TaskStateEntered",
            "timestamp": "2026-04-24T13:20:12+00:00",
            "stateEnteredEventDetails": {"name": "Normalize exception"},
        },
        {
            "type": "TaskStateExited",
            "timestamp": "2026-04-24T13:20:13+00:00",
            "stateExitedEventDetails": {
                "name": "Normalize exception",
                "output": json.dumps(normalized),
            },
        },
    ]
    rows = _summarize_history(events)
    assert rows[0]["state_name"] == "Normalize exception"
    assert rows[0]["output"]["normalization"]["source_break"]["raw_record"]["brk_cd"] == "SSI_MISS"


def test_restricted_counterparty_is_not_eligible():
    case = normalize_exception(get_data_store().exception_by_case_key("restricted_counterparty"))
    scope = scope_and_severity(case)
    assert scope["eligible"] is False
    assert "Restricted counterparty requires escalation" in scope["eligibility_reasons"]


def test_stale_reference_data_recommendation_is_actionable(monkeypatch):
    monkeypatch.setenv("DISABLE_STRANDS_MODEL_CALL", "1")
    case = normalize_exception(get_data_store().exception_by_case_key("stale_reference_data"))
    agent = run_triage(case)
    recommendation = agent["recommendation"]
    assert recommendation["recommended_queue"] == "REFERENCE_DATA_REMEDIATION"
    assert "Refresh the stale security/counterparty reference-data mapping" in recommendation["recommended_action"]
    assert "analyst_summary" in recommendation
    assert "decision_rationale" in recommendation
    assert "recommended_next_steps" in recommendation
    assert "open_questions" in recommendation
    assert "risk_flags" in recommendation
    assert "key_evidence" in recommendation
    assert any(item["label"] == "SSI/reference record" for item in recommendation["key_evidence"])
    assert recommendation["suggested_sla_minutes"] == 60


def test_near_cutoff_escalates():
    case = normalize_exception(get_data_store().exception_by_case_key("near_cutoff_escalation"))
    scope = scope_and_severity(case)
    assert scope["eligible"] is False
    assert scope["severity"] == "CRITICAL"


def test_summary_model_root_cause_is_kept_in_controlled_taxonomy(monkeypatch):
    case = normalize_exception(get_data_store().exception_by_case_key("settlement_status_mismatch"))

    def verbose_model_summary(_prompt, _payload):
        return {
            "summary": "Model-generated settlement status mismatch summary.",
            "likely_root_cause_category": (
                "Settlement instruction or status synchronization failure between internal and external systems"
            ),
            "evidence_needs": ["trade", "settlement", "prior_case", "playbook"],
            "trace": {"source": "model"},
        }

    monkeypatch.setattr(summary_agent, "run_strands_json", verbose_model_summary)
    summary = summary_agent.run_summary_stage(case)

    assert summary["summary"] == "Model-generated settlement status mismatch summary."
    assert summary["likely_root_cause_category"] == "CONFLICTING_SETTLEMENT_STATUS"
    assert summary["trace"]["stage"] == "summary"


def test_runtime_uses_controlled_root_cause_for_gateway_prior_case_lookup(monkeypatch):
    monkeypatch.setenv("DISABLE_STRANDS_MODEL_CALL", "1")
    monkeypatch.setenv("AGENTCORE_GATEWAY_LOCAL_MODE", "1")
    case = normalize_exception(get_data_store().exception_by_case_key("settlement_status_mismatch"))

    def verbose_model_summary(_prompt, _payload):
        return {
            "summary": "Model-generated settlement status mismatch summary.",
            "likely_root_cause_category": (
                "Settlement instruction or status synchronization failure between internal and external systems"
            ),
            "evidence_needs": ["trade", "settlement", "prior_case", "playbook"],
        }

    monkeypatch.setattr(summary_agent, "run_strands_json", verbose_model_summary)
    result = run_triage(case)

    assert result["summary"]["likely_root_cause_category"] == "CONFLICTING_SETTLEMENT_STATUS"
    assert result["playbook_match"]["root_cause_category"] == "CONFLICTING_SETTLEMENT_STATUS"
    assert result["recommendation"]["playbook_id"] == "PB-SETTLE-001"
