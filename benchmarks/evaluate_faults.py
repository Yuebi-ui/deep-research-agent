"""Aggregate observed faults; planned cases are not counted as recovered."""
from __future__ import annotations
import argparse
import json
import sys
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from benchmarks.metrics import percentile
from benchmarks.run_offline import read_jsonl


def evaluate(scenarios: list[dict], observations: list[dict]) -> dict:
    ids=[row['id'] for row in scenarios]
    if len(set(ids))!=len(ids):
        raise ValueError('duplicate fault scenario')
    permitted=set(ids)
    found=set()
    # Synthetic examples and real executed observations must never be conflated.
    origins={row.get('data_origin', 'real_runtime') for row in observations}
    if len(origins)>1 or not origins.issubset({'real_runtime','synthetic_example'}):
        raise ValueError('mixed or unknown fault data origins')
    origin=next(iter(origins), None)
    scenario_types={row['id']:row['fault_type'] for row in scenarios}
    for row in observations:
        scenario=row.get('scenario_id')
        if scenario not in permitted or scenario in found:
            raise ValueError('unexpected or duplicate fault observation')
        found.add(scenario)
        if row.get('recovered') not in (True,False):
            raise ValueError('each observation must record recovered as a bool')
        for name in ('lost_jobs','duplicate_writes'):
            count=row.get(name)
            if not isinstance(count,int) or isinstance(count,bool) or count<0:
                raise ValueError('fault observations need nonnegative integer integrity counts')
        duration=row.get('recovery_seconds')
        if duration is not None and (isinstance(duration,bool) or not isinstance(duration,(int,float)) or not 0<=duration<1e9):
            raise ValueError('invalid recovery_seconds')
        if 'idempotency_violations' in row:
            count=row['idempotency_violations']
            if not isinstance(count,int) or isinstance(count,bool) or count<0:
                raise ValueError('invalid idempotency_violations')
    durations=[float(row['recovery_seconds']) for row in observations if row.get('recovery_seconds') is not None]
    return {'result_kind': ('ILLUSTRATIVE_SYNTHETIC_EXAMPLE' if origin=='synthetic_example' else
                           'MEASURED_FAULT_INJECTION' if observations else 'NOT_EXECUTED'),
            'data_origin':origin,
            'planned_cases':len(scenarios), 'observed_cases':len(observations),
            'recovered_count':sum(row['recovered'] for row in observations),
            'recovery_rate':round(sum(row['recovered'] for row in observations)/len(observations),6) if observations else None,
            'lost_jobs':sum(row['lost_jobs'] for row in observations) if observations else None,
            'duplicate_writes':sum(row['duplicate_writes'] for row in observations) if observations else None,
            'recovery_p50_seconds':round(percentile(durations,0.5),4) if durations else None,
            'recovery_p95_seconds':round(percentile(durations,0.95),4) if durations else None,
            'coverage_by_type':dict(Counter(s['fault_type'] for s in scenarios)),
            'observed_by_type':dict(Counter(scenario_types[r['scenario_id']] for r in observations)),
            'recovered_by_type':dict(Counter(scenario_types[r['scenario_id']] for r in observations if r['recovered'])),
            'idempotency_violations': (sum(r['idempotency_violations'] for r in observations)
                                       if observations and all('idempotency_violations' in r for r in observations)
                                       else None),
            'manual_intervention_count': sum(r.get('status_after_observation')=='dead_letter_manual_replay_required'
                                             for r in observations),
           }


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--scenarios',type=Path,default=Path(__file__).parent/'datasets'/'fault_scenarios.v1.jsonl')
    p.add_argument('--observations',type=Path,default=None)
    p.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'artifacts'/'benchmarks'/'faults.json')
    args=p.parse_args(argv)
    scenarios=read_jsonl(args.scenarios)
    rows=read_jsonl(args.observations) if args.observations else []
    result=evaluate(scenarios,rows)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n','utf-8')
    print(f"{result['observed_cases']}/{result['planned_cases']} fault runs observed")
    return 0

if __name__=='__main__':
    raise SystemExit(main())
