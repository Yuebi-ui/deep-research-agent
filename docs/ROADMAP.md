# Roadmap

The next work is ordered by engineering risk, not by feature novelty.

## Near-term: production hardening

- Add tenant/user scoping to report, structured and episodic memory.
- Define retention and deletion semantics across SQLite and Chroma.
- Add metrics for memory recall hit rate, stale-fact usage and outbox age/dead jobs.
- Run failure-injection tests with real Redis/Chroma processes.
- Re-run external-provider E2E on the latest Memory 3.0 snapshot.
- Add an operator-facing view for temporal review candidates and dead outbox jobs.

## Quality evaluation

Track more than end-to-end latency:

- evidence-supported claim rate;
- citation correctness;
- relevant memory recall@k;
- stale-memory misuse rate;
- repeated-search reduction;
- input-token change from stage recall;
- outbox retry/dead-letter rate.

## Memory 3.0 item 8: advanced knowledge graph / A-MEM experiment

**Not implemented today.** Candidate experiment:

1. Represent evidence-backed entity relationships explicitly.
2. Keep inferred/model-suggested links separate from source-backed links.
3. Evaluate whether multi-hop retrieval improves research quality over the current
   structured Claim/Evidence + hybrid retrieval approach.
4. Only then decide whether a graph database or Graphiti-like runtime is justified.

The experiment should be rejected if it increases hallucinated associations or adds
operational complexity without measurable retrieval/quality gains.
