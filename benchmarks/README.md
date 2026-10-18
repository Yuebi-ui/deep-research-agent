# Benchmarks and ablations

This directory contains runnable evaluation tooling. The project runtime summary in
`results/live/current/` is transcribed from root `README.md` (project-owner reported); its raw runs
are unavailable in this archive, so these scripts cannot independently reproduce those metrics.
The public fixtures include invented organizations and `.example` URLs to avoid

## Data (public and versioned)

| File | Size | Purpose | Evidence status |
| --- | ---: | --- | --- |
|  `datasets/research_tasks.jsonl` | 60 tasks | Multidomain research prompts, required coverage aspects and source minimums | Prompts only; not executed |
|  `datasets/memory_corpus.jsonl` | 360 passages | Fictional section records with repeated entities, aliases, dates and deliberately similar hard negatives | Synthetic |
|  `datasets/memory_queries.jsonl` | 120 queries | Explicit relevant IDs and hard negatives; includes Chinese and English queries | Synthetic labels |
|  `datasets/citation_contract.jsonl` | 32 cases | URL / numbering validator regression, including unsupported-claim counterexamples | Synthetic |
|  `datasets/fault_scenarios.jsonl` | 40 cases | Lease, process termination, queue and outbox failure scenarios | Planned, **not executed** |

The fixed test cases are created by `fixtures/build_public_datasets.py` and
`fixtures/build_citation_cases.py`. They can be reviewed individually and
regenerated; versioning their contents avoids invisible changes to the test set.
The scenarios intentionally include forecasts mixed with observations, multiple
dates for one metric, and similarly worded reports under one entity.

## Run everything that works without API keys

From the repository root, on Python 3.12 or newer:

```bash
python -m unittest discover -s benchmarks/tests -v
python benchmarks/run_ablation.py --mode offline
python benchmarks/evaluate_citations.py
python benchmarks/evaluate_faults.py
python benchmarks/publish_offline.py --verify
```

Outputs from ordinary runs go to ignored `artifacts/benchmarks/` by default.
`results/offline/current/` is a **committed, deterministically generated** snapshot;
`publish_offline.py --verify` fails when its data/code fingerprints are stale.

### Offline retrieval variants

1. `bm25_local`: document BM25 baseline.
2. `char_tfidf_local`: local character feature cosine similarity.
3. `rrf_bm25_char`: feed the first two ranked candidate lists into
   **the project's real** `deep_research.memory.retrieval.fuse_records`.
4. `rrf_bm25_char_entity`: same RRF, with a name/alias candidate channel.

**Important:** these are offline lexical **proxies**, not Chroma dense vectors.
The real project fuses dense + keyword candidates; a lexical proxy stress test

reproduce fusion, ranking, and per-report caps that may hurt recall.

## Run research tasks after local services work

Runtime ablation flag presets are in `configs/runtime_ablation.json`. Each
preset now includes read/write master gates and a **separate** `DR_MEMORY_DATA_DIR`.
Apply **every** setting to the real API and Worker process, including any
dedicated memory worker. In `memory_off`, the legacy memory lookup and both
Outbox/fallback writers are disabled; setting `DR_MEMORY_OUTBOX_ENABLED=off`
**alone** does NOT disable writes, because it enables the legacy fallback.

Because the four directories are independent, seed the *same initial corpus*
into the memory-enabled variants before comparing retrieval; otherwise their
starting memories differ. Keep separate task DBs and Redis namespaces/instances
for completely isolated experiments; a shared task DB also holds pending Outbox
jobs and temporal-review decisions. Drain old Outbox jobs **before** switching
variants, stop all workers, and do not run different variants concurrently
against one shared task DB.

`scripts/run_baseline.py --variant <preset_name>` now performs fail-fast
verification of the *actual Worker process environment*, while
`benchmarks/run_live.py` only records operator-attested labels and cannot
verify remote Worker flags. For rigorous measurements, use the baseline runner
or independently capture/attest the deployed Worker configuration.

Restart the API and Worker services after applying each preset. Keep the same model,
embedding identity, prompt, sources, sampling settings and task set, then run:

```bash
# Starts exactly one REAL research task; may incur provider charges.
python benchmarks/run_live.py --variant memory_off --provider-kind live --limit 1 --confirm-live
# Apply and restart Workers with the candidate preset before the next run.
python benchmarks/run_live.py --variant stage_plus_episodic --provider-kind live --limit 1 \
  --output artifacts/benchmarks/candidate_task_runs.jsonl --confirm-live
```

The runner defaults to `--provider-kind fake` and labels Fake Provider runs separately;
only set `--provider-kind live` when the actual running API uses real providers.
Each run writes JSONL observations locally. `run_live.py` collects HTTP status,
elapsed time and report structure. It **does not guess** token cost, tool calls,
claim support, citation grounding or report correctness. For accurate token and
tool numbers, join task IDs/run IDs with the project's existing
`scripts/run_baseline.py` and `baseline_metrics` artifacts, then independently
annotate fact/citation quality in the JSONL record. Unavailable fields stay
`null`. The existing baseline metrics can be merged (without inventing missing
provider usage or treating an estimated cloud cost as total billed cost):

```bash
python benchmarks/import_runtime_metrics.py \
  --tasks artifacts/benchmarks/live_task_runs.jsonl \
  --artifact-dir artifacts/baseline
```


```bash
python benchmarks/merge_runs.py --inputs \
  artifacts/benchmarks/live_task_runs.jsonl \
  artifacts/benchmarks/candidate_task_runs.jsonl \
  --output artifacts/benchmarks/combined_task_runs.jsonl
python benchmarks/evaluate_runs.py \
  --input artifacts/benchmarks/combined_task_runs.jsonl \
  --baseline memory_off --candidate stage_plus_episodic
python benchmarks/run_ablation.py --mode presets
```

`evaluate_runs.py` accepts repeated rows uniquely identified by `(task_id, variant, trial_id)`, checks audit

paired `(task_id, trial_id)` keys for comparisons, and preserves missing metrics as `null`.
Without independent review, task *completion* is **not** task *success*.


## Benchmark upgrade: live Chroma + independent review + repeated trials

The available entrypoints for benchmark upgrades are listed below; the archived upgrade guide is not included in this repository.
New entrypoints: `run_chroma_retrieval.py`, `evaluate_report_quality.py`, and
`analyze_trials.py`. These scripts do not by themselves establish the independently reproducible history
of the reported metrics in `results/live/current/`. The Chroma runner has a fake-embedding Chroma smoke path and a
separate opt-in live-embedding path. The report evaluator accepts independent
annotations keyed to each report SHA-256; it never self-labels a completed
HTTP request as a successful research task. `run_live.py` can now attach
`trial_id`, per-run report artifacts and operator-supplied config hashes.
Repeated trials are paired by `(task_id,trial_id)`; confidence intervals resample
**tasks** as clusters rather than treating trials as independent samples.

## Fault injection

 `fault_scenarios.jsonl` enumerates unexecuted scenarios. After injecting
faults in a real Redis/Worker environment, write one observation per scenario:

```json
{"scenario_id":"research_worker_termination-01","recovered":true,"lost_jobs":0,"duplicate_writes":0,"recovery_seconds":12.4}
```


```bash
python benchmarks/evaluate_faults.py \
  --observations artifacts/benchmarks/observed_faults.jsonl
```

Until that file exists, `results/offline/current/fault_plan_status.json` contains


## Protocol and reproducibility

See [result status](../results/README.md) and [project test policy](../docs/TESTING.md).

A/B comparison requires a fixed task set with IDs, the same model and model
parameters, code/config snapshot, dated source snapshots where possible, and
multiple trials for stochastic agent behavior. Do not compare these local

## Complete *synthetic* reporting reference (separate from README-reported results)

When you need an example showing **every aggregate field filled**, rather
than the small 6-task schema demonstration, use
[`results/examples/reference/`](../results/examples/reference/README.md):

```bash
python benchmarks/fixtures/build_full_reference_results.py
python benchmarks/fixtures/build_full_reference_results.py --verify
python -m unittest discover -s benchmarks/tests -v
```

Historical README aggregate transcription check: `python benchmarks/verify_readme_results.py`.
