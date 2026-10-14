# Quality Gate V1 — independent Evaluator / Red Team before HITL

Phase 1 implementation (opt-in, intended for staged rollout). This changes the **speculative** graph only. It deliberately does **not** implement the Phase 2 automatic-research loop.

## Enable or roll back

```bash
# New jobs: Draft + Research in parallel, then quality review, then HITL
export DR_SEED_LITE_SPECULATIVE=on
export DR_QUALITY_GATE_V1=on

# Roll back new jobs while existing V1 jobs keep their checkpointed topology
export DR_QUALITY_GATE_V1=off
```

Default `DR_QUALITY_GATE_V1=off`. Serial jobs (`DR_SEED_LITE_SPECULATIVE=off`) retain their old behavior regardless of the V1 flag. Flag changes affect **new tasks only** after the Brief has checkpointed `quality_gate_version` (0 legacy, 1 V1). Restart workers after enabling the flag for a clean rollout; the flag is read on new graph construction and research brief execution.

## V1 graph

```text
Research Brief (checkpoints research_generation + quality_gate_version)
             /                                   \
      Draft Agent                            Supervisor + Researchers
             \                                   /
              [AND join: both branches completed]
                           |
                   evaluate_research
         (read fenced, QUARANTINED Findings)
                           |
                    red_team_audit
                           |
                human_review (HITL)
                   /               \
         approve /                   \ revise
           |                          |
   fenced_accept_update        regenerate_research
           |                    (generation += 1)
    claim_verification                |
           |                       evaluate_research
    final_report_generation         (same quality loop, new findings)
           |
       Final Report
```

- Evaluator and Red Team assess **Brief + Draft + candidate Research Findings**. Draft is the initial outline, not a fabricated authority. The Supervisor in V1 only conducts research; it does **not** run `refine_draft_report` or the old child `red_team` exit review. Final Writer still synthesizes the final report from Draft + accepted Findings.
- `speculative_research` remains quarantined. `checked_candidate_notes` validates active generation, brief fingerprint and note types **before exposing candidate notes**. Approval is the only point where `fenced_accept_update` copies notes to the authoritative downstream channel.
- Scores/diagnostics are persisted in `quality_review`, adversarial feedback in `red_team_review`, both tagged with generation. New HITL interrupt and `task.waiting_review` SSE payloads include these results plus capped candidate-note previews. Draft and previous public SSE keys remain present for compatibility.
- HITL actions in V1 phase 1 remain **approve** or **revise**. Approve proceeds to existing claim verification + final writer. Revise updates Draft, invalidates the old generation, reruns research, then reruns both reviewers before returning to HITL. *Even a purely structural edit still causes full regeneration in V1.*
- Low quality scores and Red Team concerns **do not trigger automatic extra research** in V1. They are shown to the human; targeted supplemental research and distinct `more_research` will be added in Phase 2.
- Evaluator/Red Team **model failure** is recorded as `status: error` and shown to HITL, never silently recorded as `PASS`. A bad/stale research generation instead fails closed with `ResearchFenceError`.
- This does not move claim verification: the existing component still checks the Draft before Final Writer. Final-report claim-level verification and self-healing remain separate future work.
- Historic `reject` HTTP review values were previously silently interpreted as `approve`. V1 removes this unsafe alias: the review API now accepts only `approve` and `revise`, and the runner explicitly rejects unknown actions rather than approving them.

## Validate

With project dependencies installed (`pip install -r requirements.txt -r requirements-dev.txt`) and fake providers:

```bash
pytest -q tests/test_quality_gate_v1.py
pytest -q tests/test_phase5b_reject_flow.py tests/test_phase5b_runner_lineage.py tests/test_phase5a_research_seed.py
pytest -q
```

The test suite uses `APP_ENV=test` and Fake LLM and blocks external services by default. Start with offline tests, then exercise one task with `DR_QUALITY_GATE_V1=on` in a non-production worker.

## Phase 2 implementation now available (separate opt-in)

The following improvements are implemented under `DR_QUALITY_GATE_V2=on`. See [Quality Gate V2](QUALITY_GATE_V2.md). V1 behavior remains unchanged for existing lineage checkpoints.

## Historic Phase 2 wishlist (V1 does not implement these)

- Feedback-based targeted research: Evaluator/Red Team → Supervisor → selective Researcher tasks → re-review, with max iterations and budgets.
- Support distinct HITL actions `revise_draft`, `more_research`, `cancel` with appropriate task-status transitions.
- Final-report claim verification and limited repairs; avoid duplicate whole-report rewrites.
