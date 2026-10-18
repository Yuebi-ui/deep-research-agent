"""Quality Gate V3 evaluation on *external, case-matched* gold/run JSONL.

No LLM or service calls. A checked-in SYNTHETIC example proves only metric wiring;
real improvement claims require blinded human labels + real run artifacts for both
versions. Reference configurations can be compared using independently recorded runs.

CLI:
  python -m benchmarks.quality_gate.evaluate --gold GOLD.jsonl \
    --candidate CURRENT_RUNS.jsonl --baseline REFERENCE_RUNS.jsonl --out REPORT.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        obj = json.loads(raw)
        if not isinstance(obj, dict) or not isinstance(obj.get("case_id"), str) or not obj["case_id"].strip():
            raise ValueError(f"{path}:{lineno}: missing case_id")
        if obj["case_id"] in result:
            raise ValueError(f"{path}:{lineno}: duplicate case_id")
        result[obj["case_id"]] = obj
    if not result:
        raise ValueError(f"empty evaluation file: {path}")
    return result


def _flag(obj: dict[str, Any], key: str) -> bool:
    if type(obj.get(key)) is not bool:
        raise ValueError(f"case {obj.get('case_id')}: {key} must be bool")
    return obj[key]


def _number(obj: dict[str, Any], key: str) -> float | None:
    value = obj.get(key)
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"case {obj.get('case_id')}: {key} must be nonnegative finite")
    return float(value)


def _ids(value: object, *, case_id: str, key: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"case {case_id}: {key} must be a list of nonempty IDs")
    if len(value) != len(set(value)):
        raise ValueError(f"case {case_id}: {key} has duplicate IDs")
    return set(value)


def _verdicts(value: object, *, case_id: str, key: str) -> dict[str, bool]:
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not k.strip() or type(v) is not bool
        for k, v in value.items()
    ):
        raise ValueError(f"case {case_id}: {key} must map IDs to independent bool judgments")
    return value


def summarize(gold: dict[str, dict[str, Any]], runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if set(gold) != set(runs):
        missing = sorted(set(gold) - set(runs))
        extra = sorted(set(runs) - set(gold))
        raise ValueError(f"case_id mismatch; missing={missing[:5]}, extra={extra[:5]}")
    gap_total = 0
    gap_detected = 0
    no_gap_total = 0
    false_repairs = 0
    red_required = 0
    red_detected = 0
    high_total = 0
    high_missed = 0
    red_called = 0
    false_coverage = accepted_coverage = 0
    closed_repairs = evaluated_repairs = 0
    unique_red_findings = evaluated_red_calls = 0
    coverage_complete = repairs_complete = red_complete = True
    escalated = 0
    research_actions = 0
    numeric: dict[str, list[float]] = {x: [] for x in ("llm_calls", "cost_usd", "latency_ms")}
    for case_id, target in gold.items():
        run = runs[case_id]
        is_gap = _flag(target, "requires_new_evidence")
        predicted = _flag(run, "flagged_gap")
        repaired = _flag(run, "targeted_research_called")
        called = _flag(run, "red_team_called")
        is_red = _flag(target, "red_team_expected")
        _flag(run, "human_escalated")
        severity = target.get("severity", "medium")
        if severity not in ("low", "medium", "high", "critical"):
            raise ValueError(f"invalid severity for {case_id}")
        if is_gap:
            gap_total += 1
            gap_detected += int(predicted)
            if severity in ("high", "critical"):
                high_total += 1
                high_missed += int(not predicted)
        else:
            no_gap_total += 1
            false_repairs += int(repaired)
        red_required += int(is_red)
        red_detected += int(is_red and called)
        red_called += int(called)
        # Gold labels are independent of the LLM's self-assigned coverage.
        if "covered_aspects" in run and "coverage_labels" in target:
            covered = _ids(run["covered_aspects"], case_id=case_id, key="covered_aspects")
            labels = _verdicts(target["coverage_labels"], case_id=case_id, key="coverage_labels")
            if not covered <= labels.keys():
                raise ValueError(f"case {case_id}: uncovered gold coverage judgments")
            accepted_coverage += len(covered)
            false_coverage += sum(not labels[aspect] for aspect in covered)
        else:
            coverage_complete = False

        if "repair_target_gaps" in run and "repair_gap_outcomes" in target:
            gaps = _ids(run["repair_target_gaps"], case_id=case_id, key="repair_target_gaps")
            outcomes = _verdicts(target["repair_gap_outcomes"], case_id=case_id, key="repair_gap_outcomes")
            if not gaps <= outcomes.keys():
                raise ValueError(f"case {case_id}: missing independent repair judgments")
            if gaps and not repaired:
                raise ValueError(f"case {case_id}: repair gaps without targeted research")
            evaluated_repairs += len(gaps)
            closed_repairs += sum(outcomes[gap] for gap in gaps)
        else:
            repairs_complete = False

        if "red_team_findings" in run and "unique_valid_red_team_findings" in target:
            found = _ids(run["red_team_findings"], case_id=case_id, key="red_team_findings")
            valid_unique = _ids(target["unique_valid_red_team_findings"], case_id=case_id,
                                key="unique_valid_red_team_findings")
            if found and not called:
                raise ValueError(f"case {case_id}: red findings without red team call")
            unique_red_findings += len(found & valid_unique)
            evaluated_red_calls += int(called)
        else:
            red_complete = False
        escalated += int(run["human_escalated"])
        research_actions += int(repaired)
        for key in numeric:
            value = _number(run, key)
            if value is not None:
                numeric[key].append(value)
    count = len(gold)
    def fraction(part: int, total: int) -> float | None:
        return round(part / total, 4) if total else None
    def average(key: str) -> float | None:
        return round(mean(numeric[key]), 4) if len(numeric[key]) == count else None
    return {
        "cases": count,
        "evidence_gap_recall": fraction(gap_detected, gap_total),
        "high_severity_gap_miss_rate": fraction(high_missed, high_total),
        "unnecessary_research_rate": fraction(false_repairs, no_gap_total),
        "red_team_trigger_recall": fraction(red_detected, red_required),
        "red_team_call_rate": fraction(red_called, count),
        "human_escalation_rate": fraction(escalated, count),
        "targeted_research_call_rate": fraction(research_actions, count),
        "false_evidence_coverage_rate": fraction(false_coverage, accepted_coverage) if coverage_complete else None,
        "repair_gap_closure_rate": fraction(closed_repairs, evaluated_repairs) if repairs_complete else None,
        "red_team_unique_finding_yield": round(unique_red_findings / evaluated_red_calls, 4) if red_complete and evaluated_red_calls else None,
        "average_llm_calls": average("llm_calls"),
        "average_cost_usd": average("cost_usd"),
        "average_latency_ms": average("latency_ms"),
        "labeled_gap_cases": gap_total,
        "labeled_critical_or_high_gap_cases": high_total,
        "labeled_red_team_cases": red_required,
        "missing_numeric_fields_are_null": True,
    }


def compare(gold_path: Path, candidate_path: Path, baseline_path: Path | None = None) -> dict[str, Any]:
    gold, candidate = load_jsonl(gold_path), load_jsonl(candidate_path)
    summary = summarize(gold, candidate)
    result = {
        "status": "comparison" if baseline_path is not None else "candidate_only",
        "candidate": summary,
        "baseline": None,
        "delta_candidate_minus_baseline": None,
        "limitations": [
            "Gold labels must be independently reviewed; LLM self-scores are not gold labels",
            "Structural source coverage does not prove factual or semantic correctness",
            "Missing cost/latency fields are unknown, never assumed zero",
        ],
    }
    if baseline_path is not None:
        old = summarize(gold, load_jsonl(baseline_path))
        result["baseline"] = old
        result["delta_candidate_minus_baseline"] = {
            key: round(value - old[key], 4)
            for key, value in summary.items()
            if isinstance(value, (int, float)) and type(value) is not bool
            and isinstance(old.get(key), (int, float)) and type(old[key]) is not bool
            and key not in ("cases", "labeled_gap_cases", "labeled_critical_or_high_gap_cases", "labeled_red_team_cases")
        }
    if "synthetic" in gold_path.name.lower() or "synthetic" in candidate_path.name.lower() or (
        baseline_path and "synthetic" in baseline_path.name.lower()
    ):
        result["status"] = "synthetic_smoke_only"
        result["limitations"].append("Synthetic fixture is NOT evidence of production quality improvement")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare human-labeled QG gate decisions with case-matched run results")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = compare(args.gold, args.candidate, args.baseline)
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
