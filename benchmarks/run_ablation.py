"""Convenience entry point for local retrieval ablations or real-run comparison.

Presets for E2E live runs reside in configs/runtime_ablation.v1.json and must be
applied to running workers by the operator. No live services are started here.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from benchmarks.evaluate_runs import aggregate,paired_deltas,read_rows
from benchmarks.analyze_trials import paired_analysis
from benchmarks.run_offline import main as offline_main


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['offline','real-traces','repeated-traces','presets'],default='offline')
    p.add_argument('--input',type=Path)
    p.add_argument('--baseline',default='memory_off')
    p.add_argument('--candidate',default='stage_plus_episodic')
    p.add_argument('--output',type=Path,default=ROOT/'artifacts'/'benchmarks'/'ablation.json')
    p.add_argument('--bootstrap',type=int,default=2000)
    p.add_argument('--seed',type=int,default=20261010)
    p.add_argument('--require-config-snapshots',action='store_true')
    args=p.parse_args(argv)
    if args.mode=='offline':
        return offline_main(['--output',str(args.output.parent/'offline_fixture')])
    if args.mode=='presets':
        config=json.loads((ROOT/'benchmarks'/'configs'/'runtime_ablation.v1.json').read_text('utf-8'))
        print(json.dumps(config,indent=2,ensure_ascii=False))
        return 0
    if args.input is None:
        p.error('live trace modes need --input')
    rows=read_rows(args.input)
    output=aggregate(rows)
    output['paired_comparison']=paired_deltas(rows,args.baseline,args.candidate)
    if args.mode=='repeated-traces':
        output['paired_trials']=paired_analysis(rows,args.baseline,args.candidate,seed=args.seed,
                                               bootstrap=args.bootstrap,
                                               require_config_snapshots=args.require_config_snapshots)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(f"paired run count = {output['paired_comparison']['matched_run_pairs']}")
    return 0

if __name__=='__main__':
    raise SystemExit(main())
