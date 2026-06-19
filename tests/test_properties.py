"""Property-based tests for deterministic triage gates.

These encode the three properties named in
.kiro/specs/production-ready-first-deployment/requirements.md
under "Property-Based Test Candidates" (PBT-1, PBT-2, PBT-3).
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings, strategies as st

from src.common.config import (
    CONFIDENCE_THRESHOLD,
    RESTRICTED_COUNTERPARTY_PREFIX,
    SUPPORTED_EXCEPTION_TYPES,
)
from src.common.triage_rules import (
    ROOT_CAUSE_TO_PLAYBOOK,
    ROOT_CAUSE_TO_QUEUE,
    infer_root_cause,
    normalize_exception,
    policy_confidence_gate,
    scope_and_severity,
    validate_recommendation,
)


# --- Shared strategies -------------------------------------------------------

EXCEPTION_TYPES = sorted(SUPPORTED_EXCEPTION_TYPES) + ["UNSUPPORTED_EXCEPTION", "FOO_BAR"]
PRODUCTS = ["EQUITY", "FX", "FIXED_INCOME"]
MARKETS = ["US", "EU", "APAC"]
PRIORITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


@st.composite
def exception_cases(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a normalized-exception-shaped dict.

    Mixes supported and unsupported products/markets/types, and samples both
    normal and restricted counterparty IDs, so every branch of
    ``scope_and_severity`` is exercised.
    """
    cp_id = draw(
        st.one_of(
            st.integers(min_value=1000, max_value=9999).map(lambda n: f"CP-SYN-{n}"),
            st.integers(min_value=1, max_value=999).map(
                lambda n: f"{RESTRICTED_COUNTERPARTY_PREFIX}-{n}"
            ),
        )
    )
    raw = {
        "exception_id": f"EXC-SYN-{draw(st.integers(min_value=10000, max_value=99999))}",
        "exception_type": draw(st.sampled_from(EXCEPTION_TYPES)),
        "product": draw(st.sampled_from(PRODUCTS)),
        "market": draw(st.sampled_from(MARKETS)),
        "desk": "US_CASH_EQUITIES",
        "counterparty_id": cp_id,
        "account_id": f"ACCT-SYN-{draw(st.integers(min_value=1000, max_value=9999))}",
        "settlement_date": "2025-01-15",
        "priority": draw(st.sampled_from(PRIORITIES)),
        "minutes_to_cutoff": draw(st.integers(min_value=0, max_value=600)),
    }
    # Route through normalize_exception so downstream contracts (case_key,
    # defaults) match what the rest of the system sees.
    return normalize_exception(raw)


# --- PBT-1: scope_and_severity eligibility determinism -----------------------


@given(case=exception_cases())
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_scope_and_severity_is_deterministic_and_respects_gates(case: dict[str, Any]) -> None:
    """Validates: Requirements PBT-1 (Requirement 4 AC 1-3)."""
    result = scope_and_severity(case)

    # Shape
    assert isinstance(result, dict)
    assert isinstance(result["eligible"], bool)
    assert isinstance(result["severity"], str)
    assert isinstance(result["eligibility_reasons"], list)
    assert isinstance(result["minutes_to_cutoff"], int)
    assert len(result["eligibility_reasons"]) > 0

    # Restricted counterparty forces ineligible.
    if case["counterparty_id"].startswith(RESTRICTED_COUNTERPARTY_PREFIX):
        assert result["eligible"] is False

    # Near cut-off forces ineligible + CRITICAL.
    if case["minutes_to_cutoff"] <= 30:
        assert result["eligible"] is False
        assert result["severity"] == "CRITICAL"

    # Unsupported product / market / exception type forces ineligible.
    if (
        case["product"] != "EQUITY"
        or case["market"] != "US"
        or case["exception_type"] not in SUPPORTED_EXCEPTION_TYPES
    ):
        assert result["eligible"] is False

    # Determinism: same input => same output.
    assert scope_and_severity(case) == result


# --- PBT-2: validate_recommendation totality ---------------------------------

REQUIRED_RECOMMENDATION_FIELDS = [
    "exception_id",
    "root_cause_category",
    "recommended_queue",
    "recommended_action",
    "confidence",
    "evidence_refs",
    "playbook_id",
    "human_approval_required",
    "policy_notes",
]

FIXED_CASE: dict[str, Any] = {
    "exception_id": "EXC-SYN-10042",
    "exception_type": "SETTLEMENT_INSTRUCTION_MISMATCH",
    "product": "EQUITY",
    "market": "US",
    "desk": "US_CASH_EQUITIES",
    "counterparty_id": "CP-SYN-4421",
    "account_id": "ACCT-SYN-9001",
    "settlement_date": "2025-01-15",
    "priority": "MEDIUM",
    "minutes_to_cutoff": 240,
}


@st.composite
def recommendations(draw: st.DrawFn) -> dict[str, Any]:
    """Generate recommendation dicts with a mix of valid / broken shapes."""
    root_cause = draw(st.sampled_from(list(ROOT_CAUSE_TO_PLAYBOOK.keys())))
    # Start with a fully-valid recommendation keyed to FIXED_CASE.
    rec: dict[str, Any] = {
        "exception_id": draw(
            st.sampled_from([FIXED_CASE["exception_id"], "EXC-SYN-99999", "EXC-OTHER"])
        ),
        "root_cause_category": root_cause,
        "recommended_queue": ROOT_CAUSE_TO_QUEUE[root_cause],
        "recommended_action": "do the thing",
        "confidence": draw(st.floats(min_value=0.0, max_value=1.0)),
        "evidence_refs": draw(
            st.one_of(
                st.just([]),
                st.lists(st.text(min_size=1, max_size=10), min_size=1, max_size=3),
            )
        ),
        "playbook_id": ROOT_CAUSE_TO_PLAYBOOK[root_cause],
        "human_approval_required": True,
        "policy_notes": ["Read-only tools used"],
    }
    # Randomly drop some required fields to exercise the "missing field" branch.
    drop = draw(
        st.lists(
            st.sampled_from(REQUIRED_RECOMMENDATION_FIELDS),
            max_size=3,
            unique=True,
        )
    )
    for key in drop:
        rec.pop(key, None)
    return rec


@given(recommendation=recommendations())
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_validate_recommendation_accepts_iff_shape_matches(
    recommendation: dict[str, Any],
) -> None:
    """Validates: Requirements PBT-2 (Requirement 4 AC 4)."""
    result = validate_recommendation(FIXED_CASE, recommendation)

    # Shape
    assert isinstance(result, dict)
    assert result["decision"] in {"VALID", "INVALID"}
    assert isinstance(result["accepted"], bool)
    assert isinstance(result["confidence"], float)
    assert isinstance(result["errors"], list)

    all_required_present = all(field in recommendation for field in REQUIRED_RECOMMENDATION_FIELDS)
    id_matches = recommendation.get("exception_id") == FIXED_CASE["exception_id"]
    refs = recommendation.get("evidence_refs")
    refs_ok = isinstance(refs, list) and len(refs) > 0
    root_cause = recommendation.get("root_cause_category")
    root_cause_ok = root_cause in ROOT_CAUSE_TO_PLAYBOOK
    root_cause_matches_case = root_cause == infer_root_cause(FIXED_CASE)
    playbook_ok = root_cause_ok and recommendation.get("playbook_id") == ROOT_CAUSE_TO_PLAYBOOK[root_cause]
    queue_ok = root_cause_ok and recommendation.get("recommended_queue") == ROOT_CAUSE_TO_QUEUE[root_cause]
    human_approval_ok = recommendation.get("human_approval_required") is True
    policy_notes = recommendation.get("policy_notes")
    policy_notes_ok = isinstance(policy_notes, list) and len(policy_notes) > 0

    expected_accepted = (
        all_required_present
        and id_matches
        and refs_ok
        and root_cause_ok
        and root_cause_matches_case
        and playbook_ok
        and queue_ok
        and human_approval_ok
        and policy_notes_ok
    )
    assert result["accepted"] is expected_accepted

    if result["accepted"]:
        assert result["errors"] == []
        assert result["decision"] == "VALID"
    else:
        assert len(result["errors"]) > 0
        assert result["decision"] == "INVALID"


# --- PBT-3: policy_confidence_gate completeness ------------------------------

ROOT_CAUSES_FOR_GATE = list(ROOT_CAUSE_TO_PLAYBOOK.keys()) + ["CONFLICTING_SETTLEMENT_STATUS"]


@st.composite
def gate_cases(draw: st.DrawFn) -> dict[str, Any]:
    cp_id = draw(
        st.one_of(
            st.integers(min_value=1000, max_value=9999).map(lambda n: f"CP-SYN-{n}"),
            st.integers(min_value=1, max_value=999).map(
                lambda n: f"{RESTRICTED_COUNTERPARTY_PREFIX}-{n}"
            ),
        )
    )
    return {
        "counterparty_id": cp_id,
        "minutes_to_cutoff": draw(st.integers(min_value=0, max_value=600)),
    }


@st.composite
def gate_recommendations(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "root_cause_category": draw(st.sampled_from(ROOT_CAUSES_FOR_GATE)),
        "confidence": draw(st.floats(min_value=0.0, max_value=1.0)),
    }


@st.composite
def gate_validations(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "accepted": draw(st.booleans()),
        "errors": draw(st.lists(st.text(min_size=1, max_size=20), max_size=3)),
    }


@given(
    case=gate_cases(),
    recommendation=gate_recommendations(),
    validation=gate_validations(),
)
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_policy_confidence_gate_covers_all_branches(
    case: dict[str, Any],
    recommendation: dict[str, Any],
    validation: dict[str, Any],
) -> None:
    """Validates: Requirements PBT-3 (Requirement 4 AC 2, 3, 5)."""
    result = policy_confidence_gate(case, recommendation, validation)

    assert result["decision"] in {"ROUTE_ENRICHED_CASE", "ESCALATE", "MANUAL_TRIAGE"}

    restricted = case["counterparty_id"].startswith(RESTRICTED_COUNTERPARTY_PREFIX)
    near_cutoff = case["minutes_to_cutoff"] <= 30
    conflicting = recommendation["root_cause_category"] == "CONFLICTING_SETTLEMENT_STATUS"
    low_confidence = recommendation["confidence"] < CONFIDENCE_THRESHOLD

    if not validation["accepted"]:
        assert result["decision"] == "MANUAL_TRIAGE"
        return

    # From here on validation is accepted; the gate is a priority ladder:
    # restricted > near cutoff > conflicting settlement > confidence.
    if restricted:
        assert result["decision"] == "ESCALATE"
        return
    if near_cutoff:
        assert result["decision"] == "ESCALATE"
        return
    if conflicting:
        assert result["decision"] == "ESCALATE"
        return
    if low_confidence:
        assert result["decision"] == "MANUAL_TRIAGE"
        return
    assert result["decision"] == "ROUTE_ENRICHED_CASE"
