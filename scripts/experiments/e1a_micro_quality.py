#!/usr/bin/env python
"""E1a 受控微实验：claim extractor / judge 在 thinking ON vs OFF 下的质量对照。

设计（控制变量）：

* **同一份 draft** 分别喂给 extractor(on) 与 extractor(off)，比较提取结果；
* **同一组 (claim, 搜索结果)** 分别喂给 judge(on) 与 judge(off)，比较判定。

搜索结果只抓取一次（同一证据），两边的 judge 输入完全相同 —— 避免“两次运行
搜到不同网页”污染对照。

只调用云端 API（DashScope）+ Tavily，不占本地 GPU。

    .venv/bin/python scripts/experiments/e1a_micro_quality.py --out artifacts/experiments/phase3/e1a_micro_quality.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load_fixed_draft() -> tuple[str, str]:
    """固定 draft：优先读 Phase 2 某次 run 的真实 final_report（截断到 extractor 上限）。"""
    import sqlite3

    from deep_research.settings import get_engine_settings

    db_path = get_engine_settings().resolved_data_dir / "tasks.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT thread_id, final_report FROM tasks WHERE thread_id = ?",
        ("07f5e7b123ed",),  # Phase 2 R1
    ).fetchone()
    conn.close()
    if row is None or not row["final_report"]:
        raise SystemExit("找不到固定 draft 来源（Phase 2 R1 报告）")
    return row["thread_id"], row["final_report"][:8000]


async def run_extractor_comparison(draft: str) -> dict:
    from deep_research.llm import get_chat_model_for_task
    from deep_research.utils import parse_json_response
    from deep_research.verification.claim_extractor import CLAIM_EXTRACT_PROMPT
    from langchain_core.messages import HumanMessage

    prompt = CLAIM_EXTRACT_PROMPT.format(draft_report=draft)
    results: dict[str, dict] = {}
    for label, thinking in (("on", True), ("off", False)):
        model = get_chat_model_for_task("extracting", thinking=thinking)
        t0 = time.time()
        resp = await model.ainvoke([HumanMessage(content=prompt)])
        latency = time.time() - t0
        um = resp.usage_metadata or {}
        details = um.get("output_token_details") or {}
        try:
            claims = parse_json_response(resp.content)
            parsed = [c.get("text") if isinstance(c, dict) else str(c) for c in claims]
        except Exception as exc:  # noqa: BLE001
            parsed = []
            results[label + "_parse_error"] = str(exc)
        results[label] = {
            "latency_s": round(latency, 2),
            "output_tokens": um.get("output_tokens"),
            "reasoning_tokens": details.get("reasoning"),
            "claim_count": len(parsed),
            "claims": parsed,
        }
    return results


async def run_judge_comparison(claims: list[str], verifier_factory) -> dict:
    """同一证据下的 judge ON/OFF 对照。"""
    on = verifier_factory(True)
    off = verifier_factory(False)

    # 只搜一次：同一 evidence 喂给两边
    evidence = []
    for claim in claims:
        evidence.append(await on._search_one(claim))

    results: dict[str, list] = {"on": [], "off": []}
    for label, verifier in (("on", on), ("off", off)):
        for claim, sr in zip(claims, evidence):
            t0 = time.time()
            verdict = await verifier._judge_one(claim, sr)
            results[label].append({
                "claim": claim,
                "verdict": verdict.verdict,
                "evidence_len": len(verdict.evidence or ""),
                "latency_s": round(time.time() - t0, 2),
            })
    return {"evidence": [len(e) for e in evidence], "verdicts": results}


def _summary(verdicts: dict) -> dict:
    import collections

    out = {}
    for label in ("on", "off"):
        dist = collections.Counter(v["verdict"] for v in verdicts[label])
        lats = [v["latency_s"] for v in verdicts[label]]
        out[label] = {
            "distribution": dict(dist),
            "avg_latency_s": round(sum(lats) / max(len(lats), 1), 2),
            "evidence_coverage": sum(1 for v in verdicts[label] if v["evidence_len"] > 0),
        }
    # 逐条一致性
    pairs = list(zip(verdicts["on"], verdicts["off"]))
    out["agreement"] = sum(1 for a, b in pairs if a["verdict"] == b["verdict"])
    out["total"] = len(pairs)
    out["disagreements"] = [
        {"claim": a["claim"][:60], "on": a["verdict"], "off": b["verdict"]}
        for a, b in pairs if a["verdict"] != b["verdict"]
    ]
    return out


async def main_async(out_path: Path) -> None:
    source_task, draft = _load_fixed_draft()
    print(f"[e1a] fixed draft from task {source_task} ({len(draft)} chars)")

    extractor_res = await run_extractor_comparison(draft)
    print(f"[e1a] extractor on/off claim counts: "
          f"{extractor_res['on']['claim_count']} / {extractor_res['off']['claim_count']}")

    # judge 对照：用 extractor(on) 的 claims（真实工作负载），无则用 draft 句子
    claims = extractor_res["on"]["claims"][:10] or [s.strip() for s in draft.split("。") if len(s) > 30][:10]

    from deep_research.llm import get_chat_model_for_task
    from deep_research.verification.claim_verifier import ClaimVerifier

    def verifier_factory(thinking: bool):
        v = ClaimVerifier()
        v._model = get_chat_model_for_task("verifying", thinking=thinking)
        return v

    judge_res = await run_judge_comparison(claims, verifier_factory)
    summary = _summary(judge_res["verdicts"])
    print(f"[e1a] judge agreement: {summary['agreement']}/{summary['total']} | "
          f"on={summary['on']} off={summary['off']}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "draft_source_task": source_task,
        "extractor": extractor_res,
        "judge": {"summary": summary, "raw": judge_res},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[e1a] wrote {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "artifacts" / "experiments" / "phase3" / "e1a_micro_quality.json")
    args = parser.parse_args()
    asyncio.run(main_async(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
