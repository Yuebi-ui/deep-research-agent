# GitHub publish checklist

Use this checklist before making the portfolio repository public.

## Repository metadata

Suggested repository name: `deep-research-agent`

Suggested description:

> Recoverable multi-agent deep research runtime with evidence-aware Memory 3.0, temporal claims and durable enrichment.

Suggested topics: `langgraph`, `ai-agents`, `deep-research`, `rag`, `agent-memory`, `fastapi`, `redis`, `chromadb`.

## Before the first push

```bash
python scripts/check_repo_hygiene.py
python -m compileall -q deep_research backend scripts tests
```

After installing project dependencies:

```bash
ruff check deep_research backend tests scripts/check_repo_hygiene.py
pytest -q -m "not live"
```

Also confirm that no local `.env`, `config.yml`, database, log, model artifact, or provider credential is staged.

## Public claims to keep accurate

Safe claims for the current snapshot:

- worker-owned, recoverable research execution is implemented;
- Memory 3.0 storage/retrieval/temporal/outbox foundations are implemented;
- standalone memory smoke suites cover the latest memory evolution;
- historical E2E evidence exists for an earlier hybrid runtime.

Do **not** claim yet:

- current-snapshot production validation;
- tenant-isolated memory;
- automatic temporal truth resolution;
- advanced knowledge-graph/A-MEM reasoning;
- a bundled frontend.

## GitHub presentation

1. Keep `README.md` as the landing page.
2. Pin `architecture/README.md`, `architecture/MEMORY.md`, and `docs/DESIGN_DECISIONS.md` in interview notes/bookmarks.
3. Let the included CI workflow run before sharing the repository link.
4. Add a license only after choosing the reuse terms you actually want.
5. If a credential has ever been committed to the repository history, rotate/revoke it before making the repository public; deleting it from the latest file alone is not sufficient.
