"""Attach genuine baseline_metrics/run.json observations to live task records.

Never fills unavailable tokens/cost with plausible numbers. Source run_id and
thread/task ID must match. Keeps raw observations in ignored artifacts/.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from benchmarks.evaluate_runs import read_rows,validate


def import_metrics(rows: list[dict], runs: list[dict]) -> tuple[list[dict],dict]:
    validate(rows)
    if rows[0]['data_origin'] != 'real_runtime':
        raise ValueError('cannot enrich synthetic example runs with real runtime metrics')
    by_run={}
    for run in runs:
        rid=run.get('run_id')
        if not isinstance(rid,str) or not rid or rid in by_run:
            raise ValueError('missing or duplicated baseline artifact run_id')
        by_run[rid]=run
    matched=0
    enriched=[]
    for row in rows:
        new=dict(row)
        match=by_run.get(row['run_id'])
        if match is None:
            enriched.append(new)
            continue
        if match.get('task_id')!=row.get('thread_id'):
            raise ValueError(f"task ownership mismatch for {row['run_id']}")
        validity=(match.get('integrity') or {}).get('validity')
        if validity not in (None,'VALID'):
            raise ValueError(f"invalid frozen run {row['run_id']}: {validity}")
        if match.get('status')!=row.get('status'):
            raise ValueError(f"status mismatch for run {row['run_id']}")
        metrics=match.get('llm') or {}
        quality=match.get('data_quality') or {}
        source_precision=(quality.get('precision') or {}).get('input_output_tokens_success_calls')
        complete_usage=(not metrics.get('success_without_usage')) and source_precision!='\u90e8\u5206/\u4e0d\u53ef\u7528\uff08\u89c1 notes\uff09'
        # If even one successful LLM call has no provider usage, a partial sum
        # is not the total tokens/task and cannot be used as a valid outcome.
        if complete_usage and metrics.get('calls',0)>0:
            new['input_tokens']=metrics.get('input_tokens')
            new['output_tokens']=metrics.get('output_tokens')
        search=match.get('search') or {}
        new['search_calls']=search.get('calls') if search.get('calls') is not None else None
        new['metrics_provenance']='joined_baseline_run_json_validated_identity'
        if (match.get('cloud') or {}).get('estimated_cost_rmb') is not None:
            new['estimated_cloud_cost_rmb']=(match['cloud']['estimated_cost_rmb'])
            new['cost_rmb']=None
            new['cost_note']='Estimated cloud-model spend only; not total billed cost'
        matched+=1
        enriched.append(new)
    return enriched,{'total_rows':len(rows),'matched_runs':matched,'unmatched_rows':len(rows)-matched}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--tasks',type=Path,required=True)
    p.add_argument('--artifact-dir',type=Path,default=ROOT/'artifacts'/'baseline')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts'/'benchmarks'/'enriched_runs.jsonl')
    args=p.parse_args(argv)
    files=sorted(args.artifact_dir.glob('*/run.json'))
    runs=[json.loads(f.read_text('utf-8')) for f in files]
    output,stats=import_metrics(read_rows(args.tasks),runs)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in output),'utf-8')
    print(json.dumps(stats,indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
