#!/usr/bin/env python
"""Phase 4B：把 sweep 产物汇总成决策表 + winner 判定（只读 artifacts）。

    .venv/bin/python scripts/experiments/phase4b_sweep_table.py --sweep-dir artifacts/phase4b/sweep

winner 判据（任务书 §A4）：
1. 通过全部 hard gates（failure=0 / correctness 全过 / 无 oom / 无 overflow / 无截断）；
2. PRIMARY = median burst wall（越低越好）；
3. Top-2 的 wall 差异若落在 run-to-run 噪声内 → 取并发更低、preemption 更少、
   queue 尾更低、方差更稳的那个；**不以 GPU 利用率更高为判据**。
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load_points(sweep_dir: Path) -> list[dict]:
    points = []
    for path in sorted(sweep_dir.glob("c*/rep*/summary.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_dir"] = str(path.parent.relative_to(sweep_dir))
        points.append(data)
    return points


def _fmt(value, spec="{:.1f}"):
    return "—" if value is None else spec.format(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sweep-dir", type=Path, default=REPO_ROOT / "artifacts" / "phase4b" / "sweep")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--noise-band", type=float, default=0.05,
                        help="Top-2 wall 相对差异 <= 该值视为噪声（默认 5%）")
    args = parser.parse_args(argv)

    points = _load_points(args.sweep_dir)
    if not points:
        print("[FAIL] 没有找到任何 sweep 结果", file=sys.stderr)
        return 3

    by_c: dict[int, list[dict]] = {}
    for p in points:
        by_c.setdefault(int(p["concurrency"]), []).append(p)

    print(f"{'C':>3} {'rep':>4} {'wall':>7} {'lat p50':>8} {'lat p90':>8} {'TTFT p50':>9} "
          f"{'TTFT p90':>9} {'queue p90':>10} {'KVpk':>6} {'wait':>5} {'preempt':>8} "
          f"{'gen tok':>8} {'gate':>6} {'valid':>6}")
    rows = []
    for c in sorted(by_c):
        for p in sorted(by_c[c], key=lambda x: x["rep"]):
            lat = p["latency_s"]
            ttft = p["vllm"]["ttft"]
            queue = p["vllm"]["queue"]
            gauges = p.get("gauges") or {}
            counters = p["vllm"]["counters"]
            gates_ok = bool(p["hard_gates_pass"])
            valid = gates_ok and (counters.get("preemptions") is not None)
            row = {
                "concurrency": c, "rep": p["rep"], "wall_s": p["wall_s"],
                "lat_p50": lat["p50"], "lat_p90": lat["p90"],
                "ttft_p50": ttft["p50"], "ttft_p90": ttft["p90"],
                "queue_p50": queue["p50"], "queue_p90": queue["p90"],
                "kv_peak": gauges.get("kv_peak"), "waiting_peak": gauges.get("waiting_peak"),
                "preemptions": counters.get("preemptions"),
                "gen_tokens": p.get("completion_tokens_total"),
                "throughput_req_s": p.get("throughput_req_per_s"),
                "truncated": p.get("truncated"), "incorrect": p.get("incorrect"),
                "failures": p.get("failures"),
                "gates_ok": gates_ok, "hard_gate_failures": p.get("hard_gate_failures"),
                "valid": valid, "dir": p["_dir"],
            }
            rows.append(row)
            print(f"{c:>3} {p['rep']:>4} {_fmt(row['wall_s'])} {_fmt(row['lat_p50'])} {_fmt(row['lat_p90'])} "
                  f"{_fmt(row['ttft_p50'], '{:.2f}')} {_fmt(row['ttft_p90'], '{:.2f}')} {_fmt(row['queue_p90'], '{:.2f}')} "
                  f"{_fmt(row['kv_peak'], '{:.3f}')} {str(row['waiting_peak']):>5} {str(row['preemptions']):>8} "
                  f"{str(row['gen_tokens']):>8} {'PASS' if gates_ok else 'FAIL':>6} {'yes' if valid else 'NO':>6}")
            if not gates_ok:
                print(f"      ↳ {p.get('hard_gate_failures')}")

    print("\n=== 汇总（按 C，median of reps）===")
    summary = []
    for c in sorted(by_c):
        valid_rows = [r for r in rows if r["concurrency"] == c and r["valid"]]
        all_rows = [r for r in rows if r["concurrency"] == c]
        walls = [r["wall_s"] for r in valid_rows]
        entry = {
            "concurrency": c,
            "reps": len(all_rows),
            "valid_reps": len(valid_rows),
            "wall_median": st.median(walls) if walls else None,
            "wall_min": min(walls) if walls else None,
            "wall_max": max(walls) if walls else None,
            "kv_peak_max": max((r["kv_peak"] or 0) for r in all_rows) if all_rows else None,
            "waiting_peak_max": max((r["waiting_peak"] or 0) for r in all_rows),
            "preemptions_sum": sum((r["preemptions"] or 0) for r in all_rows),
            "queue_p90_median": st.median([r["queue_p90"] for r in all_rows if r["queue_p90"] is not None]) if any(r["queue_p90"] for r in all_rows) else None,
            "lat_p90_median": st.median([r["lat_p90"] for r in all_rows if r["lat_p90"] is not None]),
        }
        summary.append(entry)
        print(f"  C={c:<3} reps={entry['reps']} valid={entry['valid_reps']} "
              f"wall_median={_fmt(entry['wall_median'])}s [{_fmt(entry['wall_min'])}–{_fmt(entry['wall_max'])}] "
              f"KVpk={_fmt(entry['kv_peak_max'], '{:.3f}')} wait_pk={entry['waiting_peak_max']} "
              f"preempt={entry['preemptions_sum']} queue_p90_med={_fmt(entry['queue_p90_median'], '{:.2f}')}")

    # winner 只在"所有 reps 都通过 hard gates"的档位之间比 —— 只通过部分 rep 的档位
    # 其 median 建立在不完整样本上，不可与全通过的档位直接排名。
    clean_points = [s for s in summary if s["reps"] > 0 and s["valid_reps"] == s["reps"]]
    excluded = [s for s in summary if s["valid_reps"] != s["reps"]]
    verdict = {"decision": "INCONCLUSIVE", "winner": None, "reason": "", "excluded": [
        {"concurrency": s["concurrency"], "valid_reps": s["valid_reps"], "reps": s["reps"]} for s in excluded]}
    if clean_points:
        ranked = sorted(clean_points, key=lambda s: s["wall_median"])
        best, second = ranked[0], (ranked[1] if len(ranked) > 1 else None)
        rel = ((second["wall_median"] - best["wall_median"]) / best["wall_median"]) if second else 1.0
        if second and rel <= args.noise_band:
            # 噪声带内 → 并发更低 → preemption 更少 → queue 尾更低 → KV 峰值更低
            tie = sorted([best, second], key=lambda s: (s["concurrency"], s["preemptions_sum"],
                                                        s["queue_p90_median"] or 0, s["kv_peak_max"] or 0))[0]
            verdict = {"decision": "KEEP", "winner": tie["concurrency"],
                       "reason": f"Top-2 差异 {rel*100:.1f}% <= 噪声带 {args.noise_band*100:.0f}%："
                                 f"C={best['concurrency']} 与 C={second['concurrency']} 视为同档，"
                                 f"按 并发更低/KV 与 queue 尾更保守 取 C={tie['concurrency']}",
                       "tie_with": [best["concurrency"], second["concurrency"]],
                       "best_wall": best["concurrency"]}
        else:
            verdict = {"decision": "KEEP", "winner": best["concurrency"],
                       "reason": f"wall_median 最小（{best['wall_median']:.1f}s，领先第二名 "
                                 f"{rel*100:.1f}% > 噪声带）"}
    else:
        verdict["reason"] = "没有任何档位在所有 reps 上通过 hard gates"
    print(f"\n=== 结论: {verdict['decision']} winner={verdict['winner']} —— {verdict['reason']}")
    if verdict.get("excluded"):
        print(f"    （未参与排名：{verdict['excluded']}）")

    payload = {"points": rows, "summary": summary, "verdict": verdict}
    out = args.out or (args.sweep_dir / "sweep_table.json")
    Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
