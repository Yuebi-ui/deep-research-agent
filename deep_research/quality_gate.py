"""Quality Gate V1/V2 routing and fenced candidate-research access.

This module is intentionally model-free: a research candidate is only readable by
pre-HITL reviewers when its generation and seed fingerprint match the active run.
Acceptance into authoritative ``notes`` remains the responsibility of the
existing ``fenced_accept_update`` helper, **after** HITL approval.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from deep_research.research_seed import (
    ResearchFenceError,
    active_generation,
    build_research_seed,
    seed_fingerprint,
    speculative_lineage,
)

QUALITY_GATE_ENV = "DR_QUALITY_GATE_V1"
QUALITY_GATE_VERSION = 1


def quality_gate_enabled() -> bool:
    """Opt-in for **new** speculative research tasks; old checkpoints stay on V0."""
    return os.environ.get(QUALITY_GATE_ENV, "off").strip().lower() in {
        "1", "true", "yes", "on",
    }


def quality_gate_lineage(state: Mapping[str, Any] | None) -> bool:
    """A checkpointed fact, never a live reading of an environment variable."""
    return bool(state) and speculative_lineage(state) and (
        type(state.get("quality_gate_version", 0)) is int
        and state.get("quality_gate_version") in (QUALITY_GATE_VERSION, QUALITY_GATE_V2_VERSION)
    )


def checked_candidate_notes(state: Mapping[str, Any]) -> list[str]:
    """Read *quarantined* findings; fail closed on stale/tampered generations.

    Never calls ``fenced_accept_update`` and never writes to the ``notes``
    channel. Checking the seed as well as generation protects review against
    a candidate from an unrelated brief accidentally being shown as current.
    """
    if not quality_gate_lineage(state):
        raise ResearchFenceError("quality gate requires a speculative V1 lineage")
    candidate = state.get("speculative_research")
    active = active_generation(state)
    if not isinstance(candidate, dict) or candidate.get("generation") != active:
        got = candidate.get("generation") if isinstance(candidate, dict) else None
        raise ResearchFenceError(
            f"quality gate rejected stale candidate: active={active} candidate={got}"
        )
    expected = seed_fingerprint(build_research_seed(state.get("research_brief", "") or ""))
    if candidate.get("seed_fingerprint") != expected:
        raise ResearchFenceError("quality gate rejected a candidate for another research brief")
    notes = candidate.get("notes")
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise ResearchFenceError("quality gate candidate notes must be a list of strings")
    return list(notes)


def format_candidate_notes(
    notes: list[str], *, max_chars: int = 14000, max_items: int = 12,
) -> str:
    """Bound judge context while retaining explicit source-item boundaries.

    These notes are untrusted research output, not instructions to the judge.
    Truncation is noted in the prompt rather than silently hiding it.
    """
    if max_chars <= 0 or max_items <= 0:
        return "[Research notes omitted by context limit]"
    if not notes:
        return "[No research findings available]"
    result: list[str] = []
    remaining = max_chars
    used = 0
    for i, note in enumerate(notes[:max_items], start=1):
        prefix = f"[Finding {i}]\n"
        if len(prefix) >= remaining:
            break
        text = str(note).strip()
        content = text[: remaining - len(prefix)]
        result.append(prefix + content)
        remaining -= len(prefix) + len(content) + 2
        used += 1
        if len(content) < len(text):
            result.append("[Remaining text truncated due to context limit]")
            break
        if remaining <= 0:
            break
    if used < len(notes):
        result.append(f"[{len(notes) - used} further findings omitted]")
    return "\n\n".join(result)


# ---------------------------------------------------------------------------
# Quality Gate V2: bounded, deterministic repair policy.  No LLM calls here.
# ---------------------------------------------------------------------------

QUALITY_GATE_V2_ENV = "DR_QUALITY_GATE_V2"
QUALITY_GATE_V2_VERSION = 2


def quality_gate_version_for_new_job() -> int:
    """V2 supersedes V1 on *new* speculative lineages only."""
    if os.environ.get(QUALITY_GATE_V2_ENV, "off").strip().lower() in {"1", "true", "yes", "on"}:
        return QUALITY_GATE_V2_VERSION
    return QUALITY_GATE_VERSION if quality_gate_enabled() else 0


def auto_research_lineage(state: Mapping[str, Any] | None) -> bool:
    return bool(state) and speculative_lineage(state) and type(state.get("quality_gate_version")) is int and (
        state.get("quality_gate_version") == QUALITY_GATE_V2_VERSION
    )


def _bounded_env_integer(name: str, default: int, maximum: int) -> int:
    """Reject pathological budgets; clamp both non-numeric and oversized values."""
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return min(max(value, 0), maximum)


def initial_repair_limits() -> dict[str, int]:
    """Stored in the Brief checkpoint so later env flips don't change budgets."""
    return {
        "quality_repair_limit": _bounded_env_integer("DR_MAX_AUTO_RESEARCH_ROUNDS", 2, 3),
        "manual_research_limit": _bounded_env_integer("DR_MAX_MANUAL_RESEARCH_ROUNDS", 2, 5),
    }


def _topics_from_reviews(quality: Mapping[str, Any], red: Mapping[str, Any]) -> list[str]:
    """Convert concrete reviewer issues to bounded, non-empty research topics."""
    topics: list[str] = []
    raw = quality.get("missing_aspects") or []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                topics.append(item.strip()[:240])
    # Red Team can flag material evidence gaps even when the evaluator passes.
    if red.get("passed") is False:
        concern = red.get("feedback")
        if isinstance(concern, str) and concern.strip() and concern.strip().upper() != "PASS":
            topics.append(f"验证红队提出的证据或矛盾：{concern.strip()[:380]}")
    unique: list[str] = []
    seen: set[str] = set()
    for topic in topics:
        key = " ".join(topic.lower().split())
        if key not in seen:
            unique.append(topic)
            seen.add(key)
        if len(unique) >= 3:
            break
    return unique


def choose_research_repair(state: Mapping[str, Any]) -> dict[str, Any]:
    """Decide whether to research again, with fail-safe reviewer and budget rules.

    This is deliberately not a graph node so its edge cases can be enumerated
    and tested without LangGraph or a model provider.
    """
    if not auto_research_lineage(state):
        raise ResearchFenceError("automated repair requires checkpointed V2 lineage")
    generation = active_generation(state)
    quality = state.get("quality_review") or {}
    red = state.get("red_team_review") or {}
    if not isinstance(quality, dict) or not isinstance(red, dict) or (
        quality.get("generation") != generation or red.get("generation") != generation
    ):
        raise ResearchFenceError("cannot route stale quality or red-team reviews")

    result: dict[str, Any] = {
        "generation": generation, "action": "human_review", "reason": "no_gap",
        "topics": [], "auto_rounds_used": state.get("quality_repair_count", 0),
        "auto_round_limit": state.get("quality_repair_limit", 2),
    }
    # A reviewer outage or malformed verdict is never 'PASS', and never a
    # request to spend more without human oversight.
    if (quality.get("status") != "completed" or red.get("status") != "completed"
            or type(quality.get("need_more_research")) is not bool
            or type(red.get("passed")) is not bool):
        result["reason"] = "reviewer_unavailable"
        return result

    evaluator_wants = quality.get("need_more_research") is True
    red_wants = red.get("passed") is False
    if not evaluator_wants and not red_wants:
        return result

    topics = _topics_from_reviews(quality, red)
    if not topics:
        result["reason"] = "no_actionable_topics"
        return result

    used = state.get("quality_repair_count", 0)
    limit = state.get("quality_repair_limit", 2)
    if type(used) is not int or type(limit) is not int or not (0 <= used <= limit <= 3):
        raise ResearchFenceError("invalid checkpointed automatic research budget")
    history = state.get("quality_repair_history") or []
    # No fresh evidence in the most recent supplement (human or auto): another
    # automatic call would likely repeat it. Escalate to HITL instead.
    if history and isinstance(history, list) and isinstance(history[-1], dict) and (
        history[-1].get("new_notes_count") == 0
    ):
        result["reason"] = "no_new_evidence"
        return result
    if used >= limit:
        result["reason"] = "auto_budget_exhausted"
        return result

    result.update({"action": "targeted_research", "reason": "evidence_gap", "topics": topics})
    return result


def merge_candidate_notes(existing: list[str], additional: list[str]) -> tuple[list[str], int]:
    """Merge *quarantined* evidence, deduplicating only whitespace-identical text.

    Distinct/conflicting source claims must survive for Red Team/Writer to see.
    """
    import re

    seen: set[str] = set()
    merged: list[str] = []
    before = 0
    for group, notes in enumerate((existing, additional)):
        if not isinstance(notes, list) or not all(isinstance(x, str) for x in notes):
            raise ResearchFenceError("supplemental research notes must be list[str]")
        for note in notes:
            text = note.strip()
            norm = re.sub(r"\s+", " ", text)
            if norm and norm not in seen:
                merged.append(text)
                seen.add(norm)
                if group == 0:
                    before += 1
    return merged, len(merged) - before


def format_followup_request(brief: str, topics: list[str], previous: list[str]) -> str:
    """Direct Supervisor at *missing* evidence; never ask for whole-report rewrite."""
    if not isinstance(topics, list) or not 1 <= len(topics) <= 3 or not all(
        isinstance(x, str) and x.strip() and len(x) <= 500 for x in topics
    ):
        raise ResearchFenceError("invalid targeted-research topics")
    items = "\n".join(f"{index}. {topic}" for index, topic in enumerate(topics, 1))
    return (
        "补充研究任务（只获取新的证据；不是要修改草稿或重写报告）：\n"
        f"研究简报：{brief[:3000]}\n"
        f"需解决的具体证据缺口：\n{items}\n"
        "请优先给出可以核实的来源、日期和具体事实；不得虚构证据。"
        "之前已有的发现仅供避免重复，不是本轮新成果：\n"
        + format_candidate_notes(previous, max_chars=3500, max_items=6)
        + "\n请使用 ConductResearch 对上述缺口执行定向补充，不要重复已覆盖的主题。"
    )
