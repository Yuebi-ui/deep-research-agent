# Testing strategy

## Principle

The default test path must not call paid external APIs. Fake providers and a network
guard make accidental external access a test failure rather than a surprise bill.

## Layers

### 1. Repository contract

```bash
python scripts/check_repo_hygiene.py
```

Checks the GitHub-facing package itself: required entry points, forbidden local
state, root-level release debris and local Markdown links.

### 2. Static checks

```bash
ruff check deep_research backend tests scripts/check_repo_hygiene.py
python -m compileall -q deep_research backend scripts tests
```

### 3. Standalone memory smoke suites

These are useful for reviewing memory behavior without bringing up Redis or external
providers:

```bash
PYTHONPATH=. python tests/offline_phase123_smoke.py
PYTHONPATH=. python tests/offline_memory3_smoke.py
PYTHONPATH=. python tests/offline_memory56_smoke.py
PYTHONPATH=. python tests/offline_memory7_temporal_smoke.py
```

They cover examples such as Chinese retrieval, idempotent report ingestion,
long-report tail recall, stage-memory distrust boundaries, episodic trace safety,
outbox lease fencing and temporal review audit.

### 4. Full pytest suite

```bash
pytest -q -m "not live"
```

This is the normal CI gate after dependencies are installed.

### 5. Live integration / E2E

Live provider tests are intentionally outside the default suite. Before calling a
snapshot production-ready, run at least:

- Redis queue/checkpoint recovery with a worker restart;
- real Chroma persistence and embedding-schema compatibility;
- one HITL resume path;
- one memory-outbox failure/retry path;
- real search/model report generation with citation inspection;
- cost/latency capture for the latest code revision.

## What tests do not prove

Passing offline tests does not prove provider quality, production throughput,
network reliability or factual correctness of generated
reports.

### Public evaluation fixtures

```bash
python -m unittest discover -s benchmarks/tests -v
python benchmarks/publish_offline.py --verify
```

🔴 These reproduce lexical ranking / project RRF fusion tests and citation parser checks on fictional records. See [benchmarks](../benchmarks/README.md) and [results](../results/README.md). They are not live Chroma or provider E2E scores.

### Quality Gate regression / evaluation

```bash
python scripts/test_quality_gate_v3_offline.py -q
python scripts/test_quality_gate_v3_p1p2_offline.py -q
python scripts/test_parallel_only_v3_offline.py -q
APP_ENV=test pytest -q tests/test_quality_gate_v3.py
```

The three Python scripts work without LangGraph dependencies, but do not replace
full graph/HITL/recovery tests. See [current V3 design](QUALITY_GATE.md).
For optional case-matched human gold versus archived baseline comparisons, use
`benchmarks/quality_gate/evaluate.py`; checked-in fixtures are SYNTHETIC only.
