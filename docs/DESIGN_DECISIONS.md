# Design decisions and trade-offs

This document is intentionally explicit about *why* the code is shaped this way.

## 1. Worker-owned graph instead of request-owned graph

**Decision:** FastAPI accepts commands and projects state; the worker owns LangGraph
execution and checkpoints.

**Why:** research calls can outlive an HTTP request and must survive client/API
restarts. Keeping execution in a worker also makes claim/heartbeat/recovery logic
one runtime concern instead of mixing it into request handlers.

**Trade-off:** more moving parts (Redis + worker) than an in-process demo.

## 2. SQLite + Redis + Chroma instead of one database for everything

**Decision:** use each store for a narrow responsibility.

- SQLite: task/report truth, human-review decisions, memory-outbox state,
  temporal-review audit.
- Redis: jobs, leases/events/checkpoints.
- Chroma: semantic retrieval indexes.
- SQLite episodic store: small operational traces that do not need embeddings.

**Why:** this keeps durable business state separate from derived search indexes.

**Trade-off:** cross-store consistency must be designed explicitly. The memory
outbox exists because a Chroma write cannot be part of the same database transaction
as task completion.

## 3. At-least-once memory enrichment instead of pretending to provide exactly-once

**Decision:** commit a durable outbox row with task completion, then process memory
at least once with lease fencing and retry.

**Why:** a process can crash after writing Chroma but before acknowledging progress.
Distributed exactly-once would require a much stronger transaction boundary.

**Compensation:** report/section/claim IDs are stable and extraction is resumable,
so replay is idempotent.

## 4. Historical memory is untrusted context

**Decision:** recalled memory is wrapped as advisory text and cannot replace current
research or verification.

**Why:** deep-research topics are time-sensitive; old reports can be stale. Retrieved
text may also contain prompt-injection-like instructions from external pages.

**Trade-off:** the agent may repeat some work rather than trusting old conclusions.
That is intentional.

## 5. Temporal candidates require review

**Decision:** the system can discover *possible* cross-report changes, but it does
not automatically mark an older claim false.

**Why:** two numbers can differ because of reporting period, scope, currency,
measurement method or a real update. Automatic supersession would turn retrieval
heuristics into truth management.

**Trade-off:** some knowledge maintenance remains human-gated.

## 6. Episodic memory stores observable traces, not chain-of-thought

**Decision:** store issued queries, source domains, tool errors and whether a finding
was emitted; do not persist hidden reasoning or model self-assessment.

**Why:** observable operations are auditable and useful for planning. Self-certified
"successful strategy" memories are much harder to trust and can amplify mistakes.

## 7. LangGraph nodes are not forced behind a BaseAgent class

**Decision:** keep graph nodes/subgraphs as functions/runnables rather than adding an
inheritance hierarchy for appearance.

**Why:** LangGraph already provides the orchestration contract. A synthetic
`BaseAgent.execute()` abstraction would add ceremony without improving state or
recovery semantics.

## 8. No graph database yet

**Decision:** keep Entity/Claim/Evidence relationships in the current structured
store and temporal audit layer; do not introduce Neo4j/Graphiti until graph queries
are proven necessary.

**Why:** current requirements are dominated by provenance, retrieval and temporal
correctness rather than arbitrary multi-hop graph traversal.

**Next experiment:** advanced knowledge-graph/A-MEM association is a roadmap item,
not a claimed feature.
