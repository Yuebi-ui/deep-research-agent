"""Commit-ready offline result publisher/CI verifier.

Generated metrics are computed from the code and deterministically regenerated
fictional fixtures. Only aggregate snapshots are published, not query-level logs.
"""
from __future__ import annotations
import argparse
import csv
import io
import hashlib
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from benchmarks.run_offline import run,read_jsonl,sha256
from benchmarks.evaluate_citations import evaluate as eval_citations
from benchmarks.evaluate_faults import evaluate as eval_faults

INPUTS=[
 'benchmarks/datasets/memory_corpus.v1.jsonl',
 'benchmarks/datasets/memory_queries.v1.jsonl',
 'benchmarks/datasets/research_tasks.v1.jsonl',
 'benchmarks/datasets/fault_scenarios.v1.jsonl',
 'benchmarks/datasets/citation_contract.v1.jsonl',
]
ENGINE=[
 'benchmarks/metrics.py',
 'benchmarks/retrievers.py',
 'benchmarks/run_offline.py',
 'benchmarks/evaluate_citations.py',
 'benchmarks/evaluate_faults.py',
 'deep_research/memory/retrieval.py',
 'deep_research/writer_validation.py',
 'benchmarks/publish_offline.py',
]


def canonical(obj: dict | str) -> str:
    if isinstance(obj,str):
        return obj
    return json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)+'\n'


def build_publication() -> dict[str,dict | str]:
    corpus=ROOT/INPUTS[0]
    query=ROOT/INPUTS[1]
    retrieval,_=run(corpus,query)
    citation_path=ROOT/INPUTS[4]
    citations,_=eval_citations(read_jsonl(citation_path))
    citations['dataset_sha256']=sha256(citation_path)
    faults=eval_faults(read_jsonl(ROOT/INPUTS[3]),[])
    manifest={
      'result_directory':'results/offline/v1',
      'provenance':'MEASURED_OFFLINE_ON_FICTIONAL_PUBLIC_FIXTURES',
      'origin':'computed_from_repository_code_with_no_LLM_Chroma_Redis_or_live_API_calls',
      'e2e_task_success_rate':None,
      'live_memory_recall_at_5':None,
      'fault_recovery_rate':None,
      'source_sha256':{name:sha256(ROOT/name) for name in INPUTS+ENGINE},
      'data_counts':{'research_prompts_not_executed':len(read_jsonl(ROOT/INPUTS[2])),
                     'memory_documents':retrieval['data_counts']['documents'],
                     'memory_queries':retrieval['data_counts']['queries'],
                     'citation_contract_cases':citations['sample_count'],
                     'fault_scenarios_not_executed':faults['planned_cases']},
      'commands':[
        'python benchmarks/publish_offline.py',
        'python benchmarks/publish_offline.py --verify',
        'python benchmarks/run_offline.py --output artifacts/benchmarks/offline_v1',
      ],
      'limits':[
        'No Chroma embedding or live provider evaluation was executed',
        'All public retrieval entities and measurements are fictional test fixtures',
        'Citation contract is not source-to-claim semantic verification',
        'Fault scenarios are a test plan, not observed recovery events',
      ],
    }
    table=io.StringIO()
    writer=csv.writer(table,lineterminator='\n')
    writer.writerow(['variant','n_queries','recall_at_1','recall_at_3','recall_at_5','mrr_at_10','ndcg_at_10'])
    for name, values in retrieval['metrics_by_variant'].items():
        writer.writerow([name,values['query_count'],values['recall_at_1'],values['recall_at_3'],
                         values['recall_at_5'],values['reciprocal_rank_at_10'],values['ndcg_at_10']])
    return {'retrieval_summary.json':retrieval,
            'ablation_summary.csv':table.getvalue(),'citation_contract.json':citations,
            'fault_plan_status.json':faults,'manifest.json':manifest}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--verify',action='store_true',help='fail if committed results differ from recomputation')
    args=p.parse_args(argv)
    destination=ROOT/'results'/'offline'/'v1'
    files=build_publication()
    mismatch=[]
    for name,content in files.items():
        path=destination/name
        expected=canonical(content)
        if args.verify:
            if not path.exists() or path.read_text('utf-8')!=expected:
                mismatch.append(name)
        else:
            destination.mkdir(parents=True,exist_ok=True)
            path.write_text(expected,'utf-8')
    if mismatch:
        print('OFFLINE RESULTS OUT OF DATE:', ', '.join(mismatch), file=sys.stderr)
        return 1
    print(f"{'verified' if args.verify else 'published'} {len(files)} deterministic result files in {destination}")
    return 0

if __name__=='__main__':
    raise SystemExit(main())
