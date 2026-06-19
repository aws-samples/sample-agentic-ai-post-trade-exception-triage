from src.evaluation.runner import run_evaluation


def test_evaluation_metrics_meet_sample_thresholds(monkeypatch, tmp_path):
    monkeypatch.setenv("DISABLE_STRANDS_MODEL_CALL", "1")
    monkeypatch.setenv("AGENTCORE_GATEWAY_LOCAL_MODE", "1")
    monkeypatch.setenv("EVALUATION_OUTPUT_DIR", str(tmp_path))
    metrics = run_evaluation()
    assert metrics["case_count"] == 8
    assert metrics["playbook_accuracy"] >= 0.85
    assert metrics["evidence_recall"] >= 0.85
    assert metrics["escalation_correctness"] == 1.0
    assert metrics["policy_denial_correctness"] == 1.0
    assert metrics["invalid_output_rate"] == 0.0
    assert metrics["unauthorized_tool_attempt_rate"] == 0.0
