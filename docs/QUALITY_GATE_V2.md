> Historical V2 document. The parallel-only V3 implementation removes serial topology and its rollback path; see PARALLEL_ONLY_V3.md for current rollout instructions.

# Quality Gate V2 — checkpointed, bounded auto-research loop

**Status:** opt-in implementation for staged validation, not production-certified. This builds on V1; the original archive and existing V0/V1 checkpoint semantics remain supported. New research occurs only when reviewers return **actionable evidence gaps**.

## Enable and rollback

```bash
# New speculative jobs ONLY: V2 takes precedence over V1.
export DR_SEED_LITE_SPECULATIVE=on
export DR_QUALITY_GATE_V2=on
# V1 may remain off; V2 supersedes it when both are on.
export DR_MAX_AUTO_RESEARCH_ROUNDS=2
export DR_MAX_MANUAL_RESEARCH_ROUNDS=2
```

Switch `DR_QUALITY_GATE_V2=off` to stop assigning V2 to **new** jobs. V2 pending tasks keep version `2` in their checkpoint and are reconstructed by the Worker with the same graph. V1 pending tasks keep version `1`; serial tasks keep the legacy graph. Restart the Worker for a controlled rollout after changing flags.

Automatic research rounds are clamped to `0..3` and explicit human-request rounds to `0..5`. The resolved per-task limits are checkpointed in `write_research_brief`; changing environment variables later cannot expand an existing task's budget.

## Graph structure

```text
Research Brief (generation 1, checkpointed quality_gate_version 2)
  |------------ Draft Agent ----------|
  |--- Supervisor + Researcher --------| (true parallel; AND join)
                                       |
                                evaluate_research
                                       |
                                red_team_audit
                                       |
                                route_quality_gate
                                  /           \
                  evidence gap + budget      sufficient / error / exhausted
                                  |                        |
                           targeted_research               |
                  (new generation, fresh child state)       |
                                  |                        |
                    merge old+new CANDIDATE notes           |
                                  |                        |
                           evaluate_research               |
                               (repeat)                    |
                                                       human_review
                                                        /   |    \
                                                approve /    |     \ more_research
                                                      /  revise_draft \ (user topic)
                               fenced_accept_update        |          |
                                       |             rewrite Draft    |
                              claim_verification           |          |
                                       |            re-evaluate       |
                               final_report_generation     |    targeted_research
                                       |                       (bounded separately)
                                 Final Report
```

`route_quality_gate` is a deterministic router, **not** another model call. It uses V2 evaluator/red-team verdicts tied to the *active research generation*. An unavailable evaluator or red team cannot trigger paid research and cannot be recorded as PASS: the workflow escalates to HITL. Without HITL, an unresolved or errored quality gate fails closed rather than auto-approving.

## When do we supplement research?

Only when both reviewers completed, at least one gives actionable evidence gaps, the task has budget left, and the most recent supplemental round did not return zero distinct findings. Missing-aspect strings and the red-team concern are bounded to three focused topics. Repeated exact/whitespace-identical findings are deduplicated; materially different or contradictory source statements are **not** deduplicated away.

`targeted_research` initializes an isolated Supervisor invocation with the original Brief, previous coverage (for avoiding repetition), and the explicit missing topics. The child is told to use `ConductResearch` for new evidence, not `refine_draft_report`; the inner red-team exit audit is bypassed so the parent V2 gate can review the actual joined Draft and Findings.

The old `speculative_research` is read through a generation+fingerprint fence before making a new request. A successful child produces a **new** candidate payload with an incremented generation and the existing plus new findings. No notes reach the authoritative `notes` channel until HITL `approve`. A stale generation or wrong child fingerprint raises `ResearchFenceError` and cannot be silently approved.

Each round records a bounded entry in `quality_repair_history` (`source`, `generation`, `requested_topics`, `new_notes_count`, and counts before/after). The newest evaluator/red team verdicts, generation, and decision are checkpointed and available in HITL and task.waiting_review events.

## HITL API behavior

`POST /api/research/{thread_id}/resume`:

| Action | V2 behavior |
| --- | --- |
| `approve` | Admit current fenced findings exactly once and proceed to claim verification + final report |
| `revise_draft` | Rewrite only the Draft, then re-run the two reviewers on the **same research generation**; do not pay to repeat Researcher calls |
| `more_research` | Require non-empty `feedback` (up to 500 chars); schedule one targeted round, merge and re-evaluate, then return to HITL |
| `revise` | Backward-compatible alias for `revise_draft` **on V2**; on V0/V1 it keeps the previous regenerate behavior |

The V2-only actions are rejected with HTTP 409 for old V0/V1 tasks, using the `human_review_v2` stage projection. `task.waiting_review` now contains `review_options`, `quality_gate_decision`, round counts, and repair history while preserving older fields. Explicit cancellation is still `POST /api/research/{thread_id}/cancel`, using the existing worker cancellation semantics; this is **not** an `approve`/`resume` action.

If the human/manual budget is exhausted, `more_research` is not offered in review options; a stale client submission returns to HITL without paid research. A human can explicitly approve an incomplete dossier, making the override an affirmative HITL decision.

## Migration and known limits

- New jobs: checkpoint version `2`; old V1 and V0 jobs retain their version, graph topology, and legacy HITL meanings. Do not change version fields in existing SQLite/Redis checkpoints manually.
- Existing SQLite **task tables require no migration**. The `task.stage=human_review_v2` projection is intentionally a new string; all other task-status transitions are unchanged.
- Since `notes` is an additive LangGraph reducer, the gate only writes **candidate payloads** until final acceptance. Do not directly mutate `notes` during automatic repair.
- V2 is **targeted-by-instruction**, not a hard guarantee that the LLM will call only specific search tools. Prompts, topic caps, result dedupe and round limits bound waste; a production-grade topic-level verification/evidence-attribution layer remains future work.
- The existing `claim_verification` still checks **Draft claims before** Final Writer, not the finished report. Final-report claim-level verification is out of scope.
- Episodic memory extraction currently selects the final research generation; initial-generation trace details may be omitted when V2 merges evidence from multiple generations. The complete approved Findings still go to Final Writer. Multi-generation episodic provenance needs a separate change.
- A Worker interrupted mid-LLM call can repeat that paid call when resuming the same pending node, but the guarded state update is atomic. Stale generations and unapproved notes remain fenced.
- Because the bundled runtime has no LangGraph dependencies and the sandbox cannot access PyPI, **the real LangGraph and Worker end-to-end test suite is not verified in this environment**. See validation commands below.

## Validation

In a minimal sandbox with Python and Pydantic (no LangGraph):

```bash
python scripts/test_quality_gate_v2_offline.py
python -m compileall -q deep_research backend scripts tests
APP_ENV=test python scripts/export_openapi.py --check
```

With complete project dependencies (`pip install -r requirements.txt -r requirements-dev.txt`):

```bash
APP_ENV=test pytest -q tests/test_quality_gate_v2.py
APP_ENV=test pytest -q tests/test_quality_gate_v1.py \
  tests/test_phase5b_reject_flow.py \
  tests/test_phase5b_runner_lineage.py \
  tests/test_api_contract.py
APP_ENV=test pytest -q
```

Before production rollout, validate SQLite checkpoint restart during an automatic supplement, V2 review action validation via HTTP, worker cancellation, and Redis task ownership under real runtime conditions. **Do not enable V2 in production until those end-to-end tests pass.**
