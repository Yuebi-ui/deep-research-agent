#!/usr/bin/env python
"""Phase 5B 汇总：配对性能/成本 + 盲评质量（只读，可复算）。

    .venv/bin/python scripts/experiments/phase5b_summarize.py \
        --analysis artifacts/phase5b/ab_analysis_all.json \
        --judge artifacts/phase5b/paired_judge.json

输入：
- ab_analysis_all.json —— `phase5a_analyze.py --prefix p5b` 的输出（每 run 一行）；
- paired_judge.json    —— `e8_paired_judge.py` 的 E2E 模式输出。

输出：配对差的分布（中位/范围）、overlap/分段 wall 汇总、cloud/token/cost、
有效性与重复分支计数、盲评每个维度的 win/loss/tie（跳过缺失数据）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DIMENSIONS = ("coverage", "unsupported", "structure", "citations", "overall")


def _load(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def _paired(analysis: list[dict]) -> dict:
    rows = {}
    for r in analysis:
        rows[(r["query"], r["round"], r["arm"])] = r

    pairs, missing = [], []
    for (q, rnd, arm) in sorted(rows):
        if arm != "ctl":
            continue
        ctl, trt = rows.get((q, rnd, "ctl")), rows.get((q, rnd, "trt"))
        if trt is None:
            missing.append(f"{q}-{rnd}")
            continue
        pairs.append({"query": q, "round": rnd, "ctl": ctl, "trt": trt})
    return {"pairs": pairs, "missing": missing, "rows": rows}


def _fmt(v, spec="{:.1f}"):
    return "—" if v is None else spec.format(v)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--analysis", type=Path,
                    default=REPO_ROOT / "artifacts" / "phase5b" / "ab_analysis_all.json")
    ap.add_argument("--judge", type=Path,
                    default=REPO_ROOT / "artifacts" / "phase5b" / "paired_judge.json")
    args = ap.parse_args(argv)

    analysis = _load(args.analysis)
    data = _paired(analysis)
    pairs = data["pairs"]
    if not pairs:
        print("[skip] 没有可配对的 ctl/trt 数据")
        return 1
    if data["missing"]:
        print(f"[warn] 缺 treatment 的配对：{data['missing']}")

    def seg(row, name):
        return (row["perf"]["segments_s"] or {}).get(name)

    deltas, overlaps = [], []
    print(f"{'pair':<12}{'ctl E2E':>9}{'trt E2E':>9}{'Δ(trt-ctl)':>11}"
          f"{'overlap':>9}{'dr wall':>8}{'rs wall':>8}{'cv':>7}{'writer':>8}")
    for p in pairs:
        ctl, trt = p["ctl"], p["trt"]
        d = trt["perf"]["e2e_s"] - ctl["perf"]["e2e_s"]
        deltas.append(d)
        ov = trt["perf"].get("draft_research_overlap_s")
        overlaps.append(ov if ov is not None else 0.0)
        tag = f"{p['query']}-{p['round']}"
        print(f"{tag:<12}{ctl['perf']['e2e_s']:>9.1f}{trt['perf']['e2e_s']:>9.1f}{d:>+11.1f}"
              f"{_fmt(ov):>9}{_fmt(seg(trt, 'write_draft_report')):>8}"
              f"{_fmt(seg(trt, 'supervisor_subgraph')):>8}"
              f"{_fmt(seg(trt, 'claim_verification')):>7}"
              f"{_fmt(seg(trt, 'final_report_generation')):>8}")

    print("\n=== 配对性能 ===")
    n = len(deltas)
    print(f"n={n}  paired Δ: median={statistics.median(deltas):+.1f}s  "
          f"range=[{min(deltas):+.1f},{max(deltas):+.1f}]  "
          f"improved={sum(1 for d in deltas if d < 0)}/{n}")
    print(f"overlap(trt): median={statistics.median(overlaps):.1f}s  "
          f">0 的比例={sum(1 for o in overlaps if o > 0)}/{n}")

    print("\n=== 成本 / 调用 / 完整性 ===")
    for arm in ("ctl", "trt"):
        rs = [r for r in analysis if r["arm"] == arm]
        if not rs:
            continue
        cost = sum(r["perf"].get("est_cost_rmb") or 0 for r in rs)
        cloud = [r["perf"].get("cloud_calls") or 0 for r in rs]
        tok_in = [r["perf"].get("supervisor_input_tokens_total") or 0 for r in rs]
        dup = sum(len(r["perf"].get("duplicate_branches") or []) for r in rs)
        invalid = sum(1 for r in rs if str(r["perf"].get("validity")) != "VALID")
        print(f"{arm}: runs={len(rs)} cloud_calls/run median={statistics.median(cloud):.0f} "
              f"cost_total={cost:.3f}RMB supervisor_in_tok median={statistics.median(tok_in):.0f} "
              f"dup_branch={dup} invalid={invalid}")

    # ---- 盲评 ----
    jp = args.judge
    if not jp.exists():
        print(f"\n[judge] 缺 {jp}（先跑 e8_paired_judge）")
        return 0
    judge = _load(jp)
    tally = {d: {"ctl": 0, "trt": 0, "tie": 0} for d in DIMENSIONS}
    confs = []
    for res in judge.get("results", []):
        vm = res.get("verdict_mapped") or {}
        for d in DIMENSIONS:
            w = vm.get(d)
            if w in tally[d]:
                tally[d][w] += 1
        conf = vm.get("confidence")
        if isinstance(conf, (int, float)):
            confs.append(conf)
    print(f"\n=== 盲评（{len(judge.get('results', []))} 对，red_team 独立评委）===")
    print(f"{'dim':<12}{'ctl':>5}{'trt':>5}{'tie':>5}")
    for d in DIMENSIONS:
        t = tally[d]
        print(f"{d:<12}{t['ctl']:>5}{t['trt']:>5}{t['tie']:>5}")
    if confs:
        print(f"judge 置信度 median={statistics.median(confs):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
