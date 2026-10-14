# V3 - Parallel-only Deep Research Graph

## What changed

- All **new** research tasks use `Research Brief -> (Draft || Supervisor+Researcher) -> AND join`.
- There is no serial graph construction path. Passing `speculative=False` to `_create_builder` raises `ValueError`.
- `DR_SEED_LITE_SPECULATIVE` is now deprecated and ignored; even `off` cannot enable serial.
- `write_research_brief` always writes `research_generation=1` and a seed message for the parallel Supervisor branch.
- `draft_agent` never writes concurrent `supervisor_messages`; this avoids LastValue conflicts.
- `human_review` no longer routes approval into `supervisor_subgraph`: approval admits generation-fenced candidate findings, then proceeds to claim verification and Final Writer.
- `quarantine_update` rejects states with `research_generation < 1` rather than directly writing candidate findings to `notes`.
- The runner rejects serial/unknown legacy post-Brief checkpoints before constructing a graph. New parallel V0/V1 checkpoints can still resume their original quality gate versions.

## Production flow

```text
Research Brief
  |------------- Draft Agent -----------------|
  |------------- Supervisor + Researcher -----|  (parallel AND-join)
                      |
             Evaluator (V2 enabled)
                      |
                  Red Team
                      |
         Quality Gate decision
          /              \
      new gaps            enough evidence or review needed
        |                            |
   Targeted Research                HITL
        |                      approve / revise_draft / more_research
    re-evaluate                         |
                                Claim Verification
                                        |
                                   Final Writer
                                        |
                                   Final Report
```

## Recommended configuration

```bash
# There is no serial-mode flag anymore.
DR_QUALITY_GATE_V2=on
DR_MAX_AUTO_RESEARCH_ROUNDS=2
DR_MAX_MANUAL_RESEARCH_ROUNDS=2
```

`DR_QUALITY_GATE_V2=off` still allows parallel V0/V1 routes for safe quality-gate rollback; it does **not** restore serial execution. V1/V0 are retained only for checkpoint compatibility and controlled rollouts.

## Old serial checkpoint warning

**No automatic migration is performed.** Serial tasks paused at HITL or waiting to run Supervisor after draft approval cannot be safely resumed on the new graph because the expected superstep/node sequence differs.

1. Before upgrading Worker(s), back up the SQLite task DB and LangGraph checkpoint DB, including their WAL files, and related Redis data if used for recovery.
2. On the V2 release, finish/cancel all serial tasks (or explicitly migrate them with a verified one-off tool) and verify none remain in-flight or awaiting review.
3. Stop old Workers, deploy V3, set V2 quality gate according to rollout policy, and only then submit new tasks.
4. V3 `TaskRunner._lineage_is_speculative` fails closed on generation 0 or post-Brief legacy checkpoints. The code does not silently convert them to parallel tasks or approve their research results.
5. If old serial checkpoints still exist, roll back to the previous V2 deployment to drain them. This V3 release cannot resume those tasks.

Historical experiment scripts comparing `DR_SEED_LITE_SPECULATIVE=on/off` are archived studies; the switch no longer changes runtime behavior.

## Validation

Run `python scripts/test_quality_gate_v2_offline.py` and `python scripts/test_parallel_only_v3_offline.py` for stdlib-only AST/node logic coverage. On a complete Python environment run the LangGraph/Worker integration tests (V0/V1/V2 parallel resumes, Redis/SQLite persistence, V2 quality-repair loop, HITL revision and approval). Production deployment should not be based on offline checks alone.
