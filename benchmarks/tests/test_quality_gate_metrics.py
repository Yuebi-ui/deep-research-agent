"""Independent labeling protocol for three Quality Gate-specific metrics."""
from copy import deepcopy
import pytest
from benchmarks.quality_gate.evaluate import summarize


def case():
    gold = {"a": {"case_id": "a", "requires_new_evidence": True,
        "red_team_expected": True, "severity": "high",
        "coverage_labels": {"policy": True, "profit": False, "cost": True},
        "repair_gap_outcomes": {"cost": True, "profit": False},
        "unique_valid_red_team_findings": ["counterexample"]},
        "b": {"case_id": "b", "requires_new_evidence": False,
        "red_team_expected": False, "severity": "low",
        "coverage_labels": {"market": True},
        "repair_gap_outcomes": {}, "unique_valid_red_team_findings": []}}
    run = {"a": {"case_id": "a", "flagged_gap": True,
        "targeted_research_called": True, "red_team_called": True,
        "human_escalated": True, "covered_aspects": ["policy", "profit", "cost"],
        "repair_target_gaps": ["cost", "profit"],
        "red_team_findings": ["counterexample", "duplicate"]},
        "b": {"case_id": "b", "flagged_gap": False,
        "targeted_research_called": False, "red_team_called": False,
        "human_escalated": True, "covered_aspects": ["market"],
        "repair_target_gaps": [], "red_team_findings": []}}
    return gold, run


def test_three_metrics_with_independent_gold():
    g, r = case()
    metrics = summarize(g, r)
    assert metrics["false_evidence_coverage_rate"] == 0.25
    assert metrics["repair_gap_closure_rate"] == 0.5
    assert metrics["red_team_unique_finding_yield"] == 1.0


def test_missing_gold_does_not_become_zero():
    g, r = case()
    del g["a"]["coverage_labels"]
    del r["a"]["repair_target_gaps"]
    del r["b"]["red_team_findings"]
    m = summarize(g, r)
    assert m["false_evidence_coverage_rate"] is None
    assert m["repair_gap_closure_rate"] is None
    assert m["red_team_unique_finding_yield"] is None


def test_reject_unreviewed_accepted_evidence():
    g, r = case()
    r["a"]["covered_aspects"].append("invented")
    with pytest.raises(ValueError, match="gold coverage"):
        summarize(g, r)


def test_reject_unverified_gap_and_fake_red_call():
    g, r = case()
    r["a"]["repair_target_gaps"].append("unknown")
    with pytest.raises(ValueError, match="repair judgments"):
        summarize(g, r)
    g, r = case()
    r["b"]["red_team_findings"].append("counterexample")
    with pytest.raises(ValueError, match="without red team call"):
        summarize(g, r)


def test_zero_denominator_returns_unknown():
    g, r = case()
    for record in r.values():
        record["covered_aspects"] = []
        record["repair_target_gaps"] = []
        record["red_team_findings"] = []
        record["red_team_called"] = False
    m = summarize(g, r)
    assert m["false_evidence_coverage_rate"] is None
    assert m["repair_gap_closure_rate"] is None
    assert m["red_team_unique_finding_yield"] is None
