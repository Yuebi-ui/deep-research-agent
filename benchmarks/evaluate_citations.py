"""Evaluate existing static citation validator's narrow contract, not support truth."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from deep_research.writer_validation import validate_report_citations
from benchmarks.run_offline import read_jsonl, sha256


def evaluate(rows: list[dict]) -> tuple[dict,list[dict]]:
    details=[]
    for row in rows:
        verdict=validate_report_citations(row['report'],row['research_material'])
        details.append({'id':row['id'],'category':row['category'],
                        'expected_static_ok':row['expected_static_ok'],
                        'actual_static_ok':verdict['ok'],
                        'matches_fixture':row['expected_static_ok']==verdict['ok'],
                        'known_semantic_support':row['known_semantic_support'],
                        'issues':verdict['issues']})
    if not rows or any(not isinstance(row['synthetic'],bool) or not row['synthetic'] for row in rows):
        raise ValueError('not a synthetic citation fixture')
    hits=sum(row['matches_fixture'] for row in details)
    semantic_mismatch=sum(row['actual_static_ok'] and row['known_semantic_support'] is False for row in details)
    return ({'result_kind':'MEASURED_STATIC_CONTRACT_ON_SYNTHETIC_CASES',
            'sample_count':len(rows),'matches_expected':hits,'mismatches':len(rows)-hits,
            'semantic_unsupported_cases_passing_static':semantic_mismatch,
            'factual_grounding_rate':None,
            'warning':'URL membership and numbering do not establish evidence support'},details)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,default=ROOT/'benchmarks'/'datasets'/'citation_contract.v1.jsonl')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts'/'benchmarks'/'citation_contract.json')
    args=p.parse_args(argv)
    summary,details=evaluate(read_jsonl(args.cases))
    summary['dataset_sha256']=sha256(args.cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'summary':summary,'cases':details},indent=2,ensure_ascii=False)+'\n','utf-8')
    print(f"static contract {summary['matches_expected']}/{summary['sample_count']} matching fixtures; {summary['semantic_unsupported_cases_passing_static']} negative support examples pass numbering")
    return 0

if __name__=='__main__':
    raise SystemExit(main())
