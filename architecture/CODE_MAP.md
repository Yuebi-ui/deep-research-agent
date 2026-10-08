# Code map

A reviewer should be able to move from an architecture box to code without hunting.

## Service/runtime layer

| Concern | Code |
|---|---|
| FastAPI app / lifecycle | `backend/main.py` |
| Research API | `backend/routes/research.py` |
| Task persistence | `backend/db/models.py`, `backend/db/repository.py` |
| Worker loop | `backend/worker.py` |
| Task runner | `backend/runtime/runner.py` |
| Redis queue | `backend/runtime/queue.py` |
| Claim fencing / heartbeat | `backend/runtime/claim.py`, `heartbeat.py` |
| Orphan recovery | `backend/runtime/reconciler.py` |
| SSE event projection | `backend/runtime/events.py`, `sse.py` |
| Durable memory outbox | `backend/runtime/memory_outbox.py` |
| Dedicated memory worker | `backend/memory_worker.py` |

## Research engine

| Concern | Code |
|---|---|
| Top-level graph | `deep_research/agent_builder.py` |
| Supervisor subgraph | `deep_research/agents/supervisor.py` |
| Researcher | `deep_research/agents/research_agent.py` |
| Draft | `deep_research/agents/draft_agent.py` |
| Evaluation / red-team | `deep_research/agents/evaluator_agent.py`, `red_team_agent.py` |
| Writer context compiler | `deep_research/writer_context.py` |
| Citation validation | `deep_research/writer_validation.py` |
| Claim verification | `deep_research/verification/` |
| Provider routing | `deep_research/llm.py` |
| Search provider construction | `deep_research/tools/search_factory.py` |

## Memory subsystem

| Concern | Code |
|---|---|
| Process-local composition root | `deep_research/memory/runtime.py` |
| High-level memory manager | `deep_research/memory/manager.py` |
| Lossless section windows | `deep_research/memory/sections.py` |
| Dense vector store | `deep_research/memory/vector_store.py` |
| Structured store | `deep_research/memory/structured_store.py` |
| Memory schemas | `deep_research/memory/schemas.py` |
| Hybrid ranking helpers | `deep_research/memory/retrieval.py` |
| Stage-aware recall | `deep_research/memory/stage_retrieval.py` |
| Episodic traces | `deep_research/memory/episodes.py` |
| Temporal review ledger | `deep_research/memory/temporal.py` |
| Embedding/schema compatibility | `deep_research/memory/embeddings.py`, `schema_guard.py`, `migration.py` |

## Migrations and operations

| Concern | Code |
|---|---|
| Task/runtime schema | `migrations/versions/0001_baseline_tasks.py`, `0002_worker_runtime_metadata.py` |
| Memory outbox + temporal audit | `migrations/versions/0003_memory_outbox_temporal.py` |
| Memory v3 backfill | `scripts/backfill_memory_v3.py` |
| Outbox backfill | `scripts/backfill_memory_outbox.py` |
| Temporal review CLI | `scripts/review_temporal_relation.py` |
| Repo contract | `scripts/check_repo_hygiene.py` |
