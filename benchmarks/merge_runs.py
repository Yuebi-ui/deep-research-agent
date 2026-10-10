"""Safely combine task JSONL files from separate A/B worker deployments."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from benchmarks.evaluate_runs import read_rows, validate, trial_id


def merge(paths: list[Path]) -> list[dict]:
    if not paths:
        raise ValueError('no input files')
    rows=[]
    for path in paths:
        rows.extend(read_rows(path))
    validate(rows)
    return sorted(rows,key=lambda item:(item['task_id'],item['variant'],trial_id(item)))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inputs',type=Path,nargs='+',required=True)
    p.add_argument('--output',type=Path,default=ROOT/'artifacts'/'benchmarks'/'combined_task_runs.jsonl')
    args=p.parse_args(argv)
    rows=merge(args.inputs)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in rows),'utf-8')
    print(f'merged {len(rows)} unique task/variant/trial observations into {args.output}')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
