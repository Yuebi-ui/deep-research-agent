# Demo flow

This is a **walkthrough of the implemented control flow**, not a canned claim that
external providers are currently available.

## 1. Submit a task

```bash
curl -X POST http://127.0.0.1:8000/api/research/start \
  -H 'Content-Type: application/json' \
  --data @examples/sample_request.json
```

The API persists a task and enqueues the task ID. It does not run the LangGraph in
the request process.

## 2. Worker executes the graph

The worker acquires a fenced claim, restores the checkpoint and runs the research
workflow. The high-level path is:

```text
brief -> draft -> [HITL] -> supervisor/research -> claim verification -> writer
```

With speculative research enabled, draft and research can fan out from the brief
and are fenced by a research generation before downstream acceptance.

## 3. Stage-aware memory recall

The brief, Supervisor and Researcher can retrieve historical memory. Recalled text
is wrapped as untrusted context and cannot replace current-task verification.

```text
semantic sections + structured claims + episodic traces
                   -> bounded stage context
```

## 4. Human review

When HITL is enabled, the task enters `waiting_review`. A reviewer can approve or
request revision; the decision is persisted before the worker resumes.

```bash
curl -X POST http://127.0.0.1:8000/api/research/<TASK_ID>/resume \
  -H 'Content-Type: application/json' \
  -d '{"action":"approve","feedback":""}'
```

## 5. Final report and durable memory enrichment

Task completion and creation of the memory-outbox job are committed together.
Memory enrichment then runs outside the user-visible critical path and can be
retried after process failure.

```text
completed report (source of truth)
          |
          +--> durable outbox --> report/section memory
                              --> episodic memory
                              --> temporal change candidates
```

## 6. Inspect results

```bash
curl http://127.0.0.1:8000/api/research/<TASK_ID>/status
curl http://127.0.0.1:8000/api/research/<TASK_ID>/report
```

For a dependency-free code review, start with
[`architecture/README.md`](../architecture/README.md) and
[`architecture/MEMORY.md`](../architecture/MEMORY.md).
