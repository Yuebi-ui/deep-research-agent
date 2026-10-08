#!/usr/bin/env python
"""E8 配对质量 judge（Phase 3C-2 §11）——辅助证据，非裁决者。

规则遵守：

* **禁止 writer 自评**：judge 用 red_team 角色（deepseek-v4-flash，与
  writer=deepseek-v4-pro 是独立模型/独立提示路径），identity 与配置全量记录；
* 位置随机化：每对报告的呈现顺序按固定 seed 打乱并记录，避免位置偏好；
* 输入两侧相同：同一 query、两组 run 的 verified claims 并集作参考；
* 输出仅为结构化 JSON 判定 + 理由。最终裁决以 e8_quality.py 的确定性指标
  为主，judge 只用于交叉验证。

    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/e8_paired_judge.py \
        --pairs e8-a1:e8-b1 e8-a2:e8-b2 e8-a3:e8-b3 \
        --out artifacts/experiments/phase3/e8_judge.json
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

ARTIFACTS = REPO_ROOT / "artifacts" / "baseline"

JUDGE_PROMPT = """你是一名独立的事实核查评审（red team）。下面有同一研究问题的两份最终报告（顺序已随机化），
以及该系统在生成报告前核查过的"已验证断言集合"（SUPPORTED/PARTIAL）。

请**只基于报告文本本身**做对比评审，逐项回答：

1. coverage：哪份报告更好地覆盖了"已验证断言集合"中的断言？
2. unsupported：哪份报告包含更少缺乏证据支持的断言（凭空数字、过度推断）？
3. structure：哪份报告结构更完整（章节清晰、逻辑连贯、结论由正文支撑）？
4. citations：哪份报告的引用与断言对应关系更好（引用支撑具体陈述，而非装饰性罗列）？
5. overall：综合哪份更好？

只输出 JSON（不要额外文字）：
{{"coverage": "1"|"2"|"tie", "unsupported": "1"|"2"|"tie", "structure": "1"|"2"|"tie",
 "citations": "1"|"2"|"tie", "overall": "1"|"2"|"tie", "confidence": 0.0-1.0,
 "reasons": "不超过 150 字的理由"}}

研究问题：{query}

===== 报告 1 =====
{report_1}

===== 报告 2 =====
{report_2}

===== 已验证断言集合（两个 run 的 SUPPORTED/PARTIAL 并集）=====
{claims}
"""


def _task_row(thread_id: str) -> dict:
    from deep_research.settings import get_engine_settings

    db = get_engine_settings().resolved_data_dir / "tasks.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE thread_id = ?", (thread_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _load_run(run_id: str) -> dict:
    run = json.loads((ARTIFACTS / run_id / "run.json").read_text(encoding="utf-8"))
    task = _task_row(run["task_id"])
    verification = json.loads(task["verification"]) if task.get("verification") else {}
    claims = [
        d.get("claim_text", "")
        for d in (verification.get("details") or [])
        if str(d.get("verdict", "")).upper() in ("SUPPORTED", "PARTIAL")
    ]
    return {
        "run_id": run_id,
        "variant": (run.get("experiment") or {}).get("variant"),
        "query": (run.get("case") or {}).get("query"),
        "report": task.get("final_report") or "",
        "supported_claims": claims,
    }


def map_verdict(verdict: dict | None, first_variant: str, second_variant: str) -> dict | None:
    """把 judge 的 1/2 标签映射回 variant（随机化已编码在 first/second 里）。"""
    if verdict is None:
        return None

    def _map(label):
        if label not in ("1", "2", "tie"):
            return None
        if label == "tie":
            return "tie"
        return first_variant if label == "1" else second_variant

    return {
        "coverage": _map(verdict.get("coverage")),
        "unsupported": _map(verdict.get("unsupported")),
        "structure": _map(verdict.get("structure")),
        "citations": _map(verdict.get("citations")),
        "overall": _map(verdict.get("overall")),
        "confidence": verdict.get("confidence"),
        "reasons": verdict.get("reasons"),
    }


def pair_specs_e2e(pair_args: list[str]) -> list[dict]:
    """E2E 配对：`<A_run>:<B_run>`。

    变体名从各 run 的 `experiment.variant` 读取（E8 的 run 就是
    thinking-on / thinking-off，标签与历史一致；Phase 5A 的 ctl/trt 同样可用）。
    """
    specs = []
    for pair in pair_args:
        first_id, second_id = pair.split(":")
        first_run, second_run = _load_run(first_id), _load_run(second_id)
        first_variant = first_run["variant"] or "variant-a"
        second_variant = second_run["variant"] or "variant-b"
        specs.append({
            "label": f"{first_id} vs {second_id}",
            "query": first_run["query"] or second_run["query"],
            "claims": sorted(set(first_run["supported_claims"]) | set(second_run["supported_claims"])),
            "reports": {first_variant: first_run["report"], second_variant: second_run["report"]},
        })
    return specs


def pair_specs_micro(micro_dir: Path, thread_id: str) -> list[dict]:
    """micro A/B：同一 writer 输入，逐 rep 配对（claim 参考集来自 checkpointer 状态）。"""
    payload = json.loads((micro_dir / "micro_ab.json").read_text(encoding="utf-8"))
    claims: list[str] = []
    query = ""
    try:
        import asyncio

        from deep_research.benchmark.quality import verification_metrics
        from scripts.experiments.e8_writer_micro_ab import _read_state  # type: ignore

        state = asyncio.run(_read_state(thread_id))
        query = (state.get("research_brief") or "")[:400]
        vm = verification_metrics((state.get("verification_report") or {}).get("details"))
        claims = vm["supported_claims"]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] micro 状态读取失败（仅影响 claims 参考集）: {exc}")

    specs = []
    for i in range(1, payload["reps_per_variant"] + 1):
        on_path, off_path = micro_dir / f"thinking-on-{i}.md", micro_dir / f"thinking-off-{i}.md"
        if on_path.exists() and off_path.exists():
            specs.append({
                "label": f"micro rep{i}",
                "query": query,
                "claims": claims,
                "reports": {
                    "thinking-on": on_path.read_text(encoding="utf-8"),
                    "thinking-off": off_path.read_text(encoding="utf-8"),
                },
            })
    return specs


def main(argv: list[str] | None = None) -> int:
    from langchain_core.messages import HumanMessage

    from deep_research.benchmark.quality import text_hash
    from deep_research.llm import get_chat_model

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pairs", nargs="+", default=None, help="E2E 模式 run 对：<on_run>:<off_run>")
    parser.add_argument("--micro-dir", type=Path, default=None, help="micro 模式：e8_writer_micro_ab 的输出目录")
    parser.add_argument("--thread", default=None, help="micro 模式的 checkpointer thread_id（取 claims 参考集）")
    parser.add_argument("--seed", type=int, default=20261006)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "experiments" / "phase3" / "e8_judge.json")
    args = parser.parse_args(argv)

    if args.micro_dir:
        if not args.thread:
            parser.error("--micro-dir 需要 --thread")
        specs = pair_specs_micro(args.micro_dir, args.thread)
    elif args.pairs:
        specs = pair_specs_e2e(args.pairs)
    else:
        parser.error("需要 --pairs 或 --micro-dir")
        return 2

    judge_model = get_chat_model("red_team")  # 独立于 writer 的 judge（identity 记录在输出里）
    identity = {"role": "red_team", "model": "deepseek-v4-flash", "provider": "openai"}

    rng = random.Random(args.seed)
    results = []
    for spec in specs:
        flip = rng.random() < 0.5
        variants = list(spec["reports"].keys())      # [A, B]，顺序来自 --pairs
        first_variant, second_variant = (
            (variants[1], variants[0]) if flip else (variants[0], variants[1])
        )
        prompt = JUDGE_PROMPT.format(
            query=spec["query"],
            report_1=spec["reports"][first_variant],
            report_2=spec["reports"][second_variant],
            claims="\n".join(f"- {c}" for c in spec["claims"]),
        )
        response = judge_model.invoke([HumanMessage(content=prompt)])
        raw = response.content if hasattr(response, "content") else str(response)

        verdict = None
        try:
            start, end = raw.find("{"), raw.rfind("}")
            verdict = json.loads(raw[start : end + 1]) if start >= 0 else None
        except Exception:
            verdict = None

        results.append({
            "pair": spec["label"],
            "order_shown": {"report_1": first_variant, "report_2": second_variant},
            "judge": identity,
            "prompt_hash": text_hash(prompt),
            "claims_reference_count": len(spec["claims"]),
            "raw_response_chars": len(raw),
            "verdict_raw": verdict,
            "verdict_mapped": map_verdict(verdict, first_variant, second_variant),
        })
        mapped = results[-1]["verdict_mapped"]
        print(f"[judge] {spec['label']} (1={first_variant}, 2={second_variant}): "
              f"overall={mapped.get('overall') if mapped else None}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"seed": args.seed, "mode": "micro" if args.micro_dir else "e2e", "results": results},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
