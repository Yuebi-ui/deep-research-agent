# Project status

This file separates **implemented code**, **offline validation** and **external
runtime evidence** so the repository does not overstate what has been proven.

## Current implementation

| Capability | Code status | Notes |
|---|---|---|
| FastAPI command/query/SSE API | Implemented | API does not own graph execution |
| Redis worker runtime + claims/recovery | Implemented | requires runtime infrastructure to exercise |
| LangGraph research / HITL / verification / writer flow | Implemented | topology is feature-flag dependent |
| Report section memory | Implemented | deterministic windows + reconstruction checks |
| Structured Claim/Evidence memory | Implemented | provenance-preserving extraction |
| Hybrid retrieval | Implemented | dense + lexical + entity signals |
| Supervisor/Researcher stage recall | Implemented | bounded and explicitly untrusted |
| Episodic research memory | Implemented | observed traces only |
| Temporal Claim review ledger | Implemented foundation | review required for authoritative decisions |
| Durable memory outbox / consolidator | Implemented foundation | retry/dead-letter/lease fencing |
| Tenant-isolated memory | Not implemented | required before shared multi-user memory |
| Advanced knowledge graph / A-MEM | Not implemented | roadmap experiment |

## Validation layers

### Standalone smoke suites

The repository contains four self-contained smoke programs for the memory evolution:

- `tests/offline_phase123_smoke.py`
- `tests/offline_memory3_smoke.py`
- `tests/offline_memory56_smoke.py`
- `tests/offline_memory7_temporal_smoke.py`

They exercise deterministic fake stores and/or local SQLite and do not constitute a
real Chroma + Redis + external-LLM end-to-end test.

### Full pytest suite

The full suite lives under `tests/` and is intended to run after project dependencies
are installed. CI is configured to run it in offline/fake-provider mode.

### External runtime evidence

`docs/E2E_EVIDENCE.md` records an earlier hybrid local/cloud end-to-end runtime.
That evidence predates the latest Memory 3.0 changes. It is useful runtime history,
but it is **not** presented as proof that the current snapshot has completed a fresh
production-like E2E run.

## Known engineering gaps

1. Shared memory has no tenant boundary/retention policy.
2. A dedicated memory worker has not been proven under sustained production load in
   this snapshot.
3. Temporal candidates still need explicit review to become authoritative changes.
4. Real provider latency/cost/quality for the latest memory paths should be measured
   again before making performance claims.
5. The repository intentionally omits a frontend implementation; it exposes REST/SSE
   contracts for external clients.
