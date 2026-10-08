# Contributing

This repository is organized around two runtime boundaries: the **research engine**
(`deep_research/`) and the **service runtime** (`backend/`). Keep new code on the
side of the boundary that owns the behavior.

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp config.server.example.yml config.yml
export APP_ENV=test ALLOW_LIVE_EXTERNAL_APIS=false
```

## Before opening a change

```bash
python scripts/check_repo_hygiene.py
ruff check deep_research backend tests scripts/check_repo_hygiene.py
python -m compileall -q deep_research backend scripts tests
pytest -q -m "not live"
```

The default test environment is intentionally offline. A test that needs network
access must be explicitly marked; do not make paid model/search calls part of the
normal test path.

## Architecture rules

- `backend/` owns API, queues, leases, persistence and worker lifecycle.
- `deep_research/` owns the LangGraph research workflow and research-domain logic.
- Agent code may consume memory; the memory package must not import graph builders
  merely to obtain runtime clients. Shared clients live in
  `deep_research.memory.runtime`.
- Historical memory is advisory context. Final report claims still require current
  task evidence and verification.
- Durable memory enrichment is at-least-once; stable IDs and resumable extraction
  make replay idempotent rather than pretending to provide distributed exactly-once.
