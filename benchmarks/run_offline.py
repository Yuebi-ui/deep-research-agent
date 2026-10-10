"""Measure deterministic LOCAL retrieval proxies using real project fusion code.

No embedding or LLM is called. This benchmark is narrower than end-to-end Agent
or Chroma/dense recall; output provenance always names it local_fixture_offline.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from benchmarks.metrics import mean_metrics, retrieval_metrics
from benchmarks.retrievers import OfflineIndex, VARIANTS

DATASETS = ROOT / 'benchmarks' / 'datasets'


def read_jsonl(path: Path) -> list[dict]:
    rows=[]
    for line_number, raw in enumerate(path.read_text('utf-8').splitlines(), 1):
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f'{path}:{line_number}: invalid JSON') from exc
    return rows


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(corpus_file: Path, query_file: Path) -> tuple[dict, list[dict]]:
    # The expanded fixtures are intentionally not tracked. Regenerate them
    # from the public deterministic generator when requested.
    if not corpus_file.exists() or not query_file.exists():
        if (corpus_file.resolve() == (DATASETS/'memory_corpus.v1.jsonl').resolve()
                and query_file.resolve() == (DATASETS/'memory_queries.v1.jsonl').resolve()):
            from benchmarks.fixtures.build_public_datasets import build
            build()
        else:
            raise FileNotFoundError(f'missing benchmark inputs: {corpus_file}, {query_file}')
    corpus=read_jsonl(corpus_file)
    queries=read_jsonl(query_file)
    if not queries or any(x.get('synthetic') is not True for x in queries):
        raise ValueError('expected a nonempty synthetic offline query fixture')
    corpus_ids={item['id'] for item in corpus}
    query_ids=[item['id'] for item in queries]
    if len(set(query_ids))!=len(query_ids):
        raise ValueError('duplicate query id')
    for q in queries:
        if not set(q['relevant_ids']).issubset(corpus_ids):
            raise ValueError(f"missing relevant document for {q['id']}")
    index=OfflineIndex(corpus)
    per_item=[]
    all_scores=defaultdict(list)
    by_category=defaultdict(lambda: defaultdict(list))
    by_language=defaultdict(lambda: defaultdict(list))
    for q in queries:
        for variant in VARIANTS:
            retrieved=index.rank(q['query'], variant, limit=10)
            measures=retrieval_metrics(q['relevant_ids'], retrieved)
            all_scores[variant].append(measures)
            by_category[(variant,q['category'])]['recall_at_5'].append(measures['recall_at_5'])
            by_language[(variant,q['language'])]['recall_at_5'].append(measures['recall_at_5'])
            per_item.append({'query_id':q['id'],'category':q['category'],
                 'language':q['language'], 'variant':variant,
                 'gold_ids':q['relevant_ids'], 'retrieved_ids':retrieved,
                 'metrics':measures,'data_origin':'computed_local_fixture'})
    summary={
      'result_kind':'MEASURED_OFFLINE_FIXTURE',
      'scope':'local_bm25_character_tfidf_and_project_fuse_records; NOT_Chroma_densely_embedded_or_Agent_E2E',
      'data_origin':'fully_synthetic_corpus_and_queries',
      'real_external_calls':0,
      'data_counts':{'documents':len(corpus),'queries':len(queries), 'variants':len(VARIANTS)},
      'corpus_sha256':sha256(corpus_file), 'queries_sha256':sha256(query_file),
      'metrics_by_variant':{name: {'query_count':len(rows), **mean_metrics(rows)} for name,rows in all_scores.items()},
      'recall_at_5_by_category':{
        v:{k:round(sum(x['recall_at_5'])/len(x['recall_at_5']),6) for (vv,k),x in by_category.items() if vv==v}
          for v in VARIANTS},
      'recall_at_5_by_language':{
        v:{k:round(sum(x['recall_at_5'])/len(x['recall_at_5']),6) for (vv,k),x in by_language.items() if vv==v}
          for v in VARIANTS},
    }
    return summary, per_item


def main(argv: list[str] | None=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, default=DATASETS/'memory_corpus.v1.jsonl')
    parser.add_argument('--queries', type=Path, default=DATASETS/'memory_queries.v1.jsonl')
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts'/'benchmarks'/'offline_fixture')
    args=parser.parse_args(argv)
    summary, rows=run(args.corpus, args.queries)
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,sort_keys=True)+'\n','utf-8')
    (args.output/'per_query.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n' for row in rows),'utf-8')
    print(json.dumps(summary['metrics_by_variant'],indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
