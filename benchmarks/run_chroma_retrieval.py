"""Benchmark production Chroma dense, keyword and RRF retrieval on labeled IDs.

Uses VectorMemoryStore.search_memory/search_keyword + fuse_records -- the same
functions as MemoryManager section retrieval, NOT BM25/TF-IDF stand-ins.
Always label the embedding provider and the provenance of the relevance labels.
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
from benchmarks.run_offline import read_jsonl
from deep_research.memory.retrieval import fuse_records

VARIANTS = ('chroma_dense', 'chroma_keyword', 'chroma_hybrid_rrf')


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def labels_origin(queries: list[dict]) -> str:
    if not queries:
        raise ValueError('no evaluation queries')
    origins = set()
    for q in queries:
        if q.get('synthetic') is True:
            origins.add('synthetic_fixture')
        elif q.get('label_origin') == 'human_independent':
            origins.add('operator_attested_human_labels')
        else:
            origins.add('unverified_labels')
    if len(origins) != 1:
        raise ValueError('cannot mix synthetic, unverified and human-labeled queries')
    return origins.pop()


def load_fixture(store, corpus: list[dict]) -> int:
    """Write a fictional fixture to an explicitly separate benchmark collection."""
    ids = [item.get('id') for item in corpus]
    if len(set(ids)) != len(ids) or not all(isinstance(x, str) and x for x in ids):
        raise ValueError('fixture IDs must be unique nonempty strings')
    if not corpus or any(item.get('synthetic') is not True for item in corpus):
        raise ValueError('fixture import requires an exclusively synthetic corpus')
    entries = []
    for i, doc in enumerate(corpus):
        if not isinstance(doc.get('content'), str) or not doc['content']:
            raise ValueError('fixture content cannot be empty')
        entries.append({
            'id': doc['id'], 'content': doc['content'],
            'metadata': {
                'report_id': doc.get('report_id') or doc['id'],
                'structured_status': 'complete',
                'section_index': i,
                'source_kind': 'fictional_fixture',
            },
        })
    store.add_memories(entries, batch_size=10)
    return len(entries)


def score_queries(store, queries: list[dict], *, k: int = 10, candidate_k: int | None = None) -> tuple[dict, list[dict]]:
    if k != 10:
        raise ValueError('k must be 10 to compute complete Recall@1/3/5/10 metrics')
    limit = min(k * 4, 24) if candidate_k is None else candidate_k
    if limit < k:
        raise ValueError('candidate_k must be >= k')
    origin = labels_origin(queries)
    query_ids = [q['id'] for q in queries]
    if len(set(query_ids)) != len(query_ids):
        raise ValueError('duplicate query IDs')
    all_gold = set()
    for query in queries:
        labels = query.get('relevant_ids')
        if not isinstance(labels, list) or not labels or len(labels) != len(set(labels)):
            raise ValueError(f"query {query['id']} missing unique relevant_ids")
        if not isinstance(query.get('query'), str) or not query['query'].strip():
            raise ValueError(f"query {query['id']} missing text")
        all_gold.update(labels)
    missing = [ident for ident in sorted(all_gold) if store.get_memory(ident) is None]
    if missing:
        raise ValueError(f'{len(missing)} labeled gold IDs not present in Chroma; first: {missing[:5]}')
    collected = defaultdict(list)
    by_language = defaultdict(lambda: defaultdict(list))
    individual = []
    for q in queries:
        # Exactly the production section eligibility filter; do not convert a
        # failed keyword channel into an apparently valid score of zero.
        dense = [x for x in store.search_memory(q['query'], top_k=limit)
                 if (x.get('metadata') or {}).get('structured_status') == 'complete']
        keyword = [x for x in store.search_keyword(q['query'], top_k=limit)
                   if (x.get('metadata') or {}).get('structured_status') == 'complete']
        hybrid = fuse_records(q['query'], [dense, keyword], limit=k, per_report=2)
        channels = {
            'chroma_dense': dense[:k],
            'chroma_keyword': keyword[:k],
            'chroma_hybrid_rrf': hybrid,
        }
        for variant in VARIANTS:
            retrieved = [item['id'] for item in channels[variant]]
            metrics = retrieval_metrics(q['relevant_ids'], retrieved)
            collected[variant].append(metrics)
            by_language[variant][q.get('language', 'unspecified')].append(metrics)
            individual.append({
                'query_id': q['id'], 'variant': variant,
                'language': q.get('language'),
                'gold_ids': q['relevant_ids'], 'retrieved_ids': retrieved,
                'metrics': metrics, 'labels_origin': origin,
            })
    summary = {
        'result_kind': 'MEASURED_CHROMA_RETRIEVAL',
        'not_agent_e2e': True,
        'labels_origin': origin,
        'collection_document_count': store.count(),
        'evaluation_queries': len(queries),
        'candidate_k': limit, 'output_k': k, 'hybrid_per_report': 2,
        'retrieval_implementation': 'VectorMemoryStore.search_memory+search_keyword+fuse_records',
        'metrics_by_variant': {v: {'query_count': len(collected[v]), **mean_metrics(collected[v])}
                               for v in VARIANTS},
        'metrics_by_language': {
            v: {language: mean_metrics(rows) for language, rows in sorted(by_language[v].items())}
            for v in VARIANTS
        },
        'interpretation': 'Human judgments are required for external relevance; fixture scores are not live-world performance.',
    }
    return summary, individual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--persist-dir', type=Path, required=True, help='Explicit Chroma path; do not use production data for fixture imports')
    parser.add_argument('--collection', default='memory_sections', help='Existing section collection to search')
    parser.add_argument('--provider-kind', choices=['fake', 'live'], default='fake')
    parser.add_argument('--confirm-embedding-spend', action='store_true')
    parser.add_argument('--queries', type=Path, default=ROOT / 'benchmarks' / 'datasets' / 'memory_queries.v1.jsonl')
    parser.add_argument('--k', type=int, default=10)
    parser.add_argument('--import-fixture', action='store_true', help='Import synthetic corpus into an isolated benchmark collection')
    parser.add_argument('--confirm-write-fixtures', action='store_true')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'benchmarks' / 'datasets' / 'memory_corpus.v1.jsonl')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts' / 'benchmarks' / 'chroma_retrieval')
    args = parser.parse_args(argv)
    if args.provider_kind == 'live' and not args.confirm_embedding_spend:
        parser.error('live Embedding calls require --confirm-embedding-spend')
    if args.import_fixture and not (args.confirm_write_fixtures and args.collection.startswith('benchmark_')):
        parser.error('fixture import requires --confirm-write-fixtures and --collection benchmark_* (never memory_sections)')
    if args.confirm_write_fixtures and not args.import_fixture:
        parser.error('--confirm-write-fixtures is only for --import-fixture')
    if args.k != 10:
        parser.error('--k must be 10 to report Recall@1/3/5/10 without truncation bias')
    queries = read_jsonl(args.queries)
    if args.import_fixture and labels_origin(queries) != 'synthetic_fixture':
        parser.error('fixture import only evaluates explicitly synthetic labels')
    # Deferred imports prevent unit tests from needing Chroma or provider keys.
    from deep_research.memory.embeddings import EmbeddingClient
    from deep_research.memory.vector_store import VectorMemoryStore
    embedder = EmbeddingClient(force_fake=(args.provider_kind == 'fake'))
    store = VectorMemoryStore(persist_dir=str(args.persist_dir), collection_name=args.collection, embedder=embedder)
    imported = load_fixture(store, read_jsonl(args.corpus)) if args.import_fixture else 0
    summary, per_query = score_queries(store, queries, k=args.k)
    summary.update({
        'embedding_provider': embedder.identity.provider,
        'embedding_model': embedder.identity.model,
        'embedding_schema_version': embedder.identity.schema_version,
        'embedding_is_fake': embedder.is_fake,
        'query_file_sha256': _fingerprint(args.queries),
        'corpus_file_sha256': _fingerprint(args.corpus) if args.import_fixture else None,
        'fixture_documents_imported': imported,
        'collection_name': args.collection,
        'data_origin': ('fake_embedding_chroma' if embedder.is_fake else 'live_embedding_chroma'),
    })
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + '\n', 'utf-8')
    (args.output / 'per_query.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n' for row in per_query), 'utf-8')
    print(f"Chroma retrieval completed: {len(queries)} queries, provider={summary['embedding_provider']}, "
          f"labels={summary['labels_origin']} -> {args.output}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
