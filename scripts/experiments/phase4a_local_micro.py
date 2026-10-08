#!/usr/bin/env python
"""Phase 4A 本地单请求 micro 基线（§19）。

对本地 vLLM 发送**代表性请求形状**（基于真实 researcher_summarizer 的
input token 分布），单请求、串行执行，测：

    latency / usage / prefill / decode / queue（后三者取自 /metrics 直方图差值）

约束（与任务书一致）：
- 不写 task DB、不写 memory、不进 production graph、不改 vLLM 配置；
- prompt 为确定性合成文本（按目标 token 数生成），不含任何真实数据；
- 每请求前后各抓一次 /metrics 快照 → 单请求的 prefill/decode/queue 可直接归属。

    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/phase4a_local_micro.py \
        --sizes 1500 5000 7000 --reps 3 --out artifacts/phase4a/micro
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from deep_research.profiling.scrape import delta_histogram, extract_snapshot, histogram_quantile  # noqa: E402
from deep_research.profiling.sampler import fetch_metrics_text  # noqa: E402


def _local_endpoint() -> tuple[str, str]:
    from deep_research.settings import get_engine_settings
    from deep_research.utils import load_config

    cfg = load_config(stage_name=get_engine_settings().stage)
    local = (cfg.get("cognition") or {}).get("openai_local") or {}
    return str(local.get("base_url") or "").rstrip("/"), str(local.get("api_key") or "")


def _make_token_counter():
    """精确 token 计数：优先 vLLM /tokenize（真实模型 tokenizer），否则字符启发式。

    背景：本地模型目录没有 tokenizer.json（context_budget 因此走保守启发式），
    但 vLLM 服务端持有完整 tokenizer，可用 /tokenize 端点精确计数。
    """
    base_url, api_key = _local_endpoint()
    if base_url:
        # /tokenize 挂在服务根路径（config 的 base_url 带 /v1 前缀，属于 OpenAI 兼容层）
        root_url = base_url[:-3] if base_url.endswith("/v1") else base_url

        def _count_via_vllm(text: str) -> int | None:
            try:
                resp = _post(root_url + "/tokenize", {"model": "qwen3-30b-a3b-local", "prompt": text}, api_key, timeout=30)
                return int(resp.get("count")) if resp.get("count") is not None else len(resp.get("tokens") or [])
            except Exception:
                return None

        if _count_via_vllm("ping") is not None:
            return _count_via_vllm
    return lambda text: max(1, len(text) // 4)


_PARAGRAPH = (
    "The system processes research notes and produces a compact summary. "
    "Each paragraph is synthetic filler used only for load-shape measurement. "
    "It contains no real user data, no secrets, and no production content. "
    "Token counts are verified with the model tokenizer before sending. "
)


def _fit_to_tokens(text: str, target: int, counter) -> tuple[str, int]:
    """把 text 收敛到不超过 target tokens（尾部字符二分裁剪，保持确定性）。"""
    count = counter(text)
    if count <= target:
        return text, count
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter(text[:mid]) <= target:
            lo = mid
        else:
            hi = mid - 1
    trimmed = text[:lo]
    return trimmed, counter(trimmed)


def _prompt_of_tokens(target: int, tokenizer_counter, *, headroom: int = 500) -> tuple[str, int]:
    """生成不超过 target tokens 的确定性合成 prompt。

    headroom：为 max_tokens 预留的余量（真实约束：prompt + output ≤ 8192）。
    """
    instruction = ("Summarize the following notes in a detailed paragraph of roughly 350 words,\n"
                   "covering every distinct topic mentioned in order.\n\n")
    body = _PARAGRAPH * max(1, target // 40)
    text = instruction + body
    count = tokenizer_counter(text)
    while count < target - headroom and len(body) < 2_000_000:
        body += _PARAGRAPH
        text = instruction + body
        count = tokenizer_counter(text)
    return _fit_to_tokens(text, target, tokenizer_counter)


def _post(url: str, payload: dict, api_key: str, timeout: float = 300.0) -> dict:
    import urllib.error

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {body}") from exc


def _snapshot() -> dict | None:
    text = fetch_metrics_text("http://127.0.0.1:8001/metrics")
    return extract_snapshot(text) if text else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1500, 5000, 7000],
                        help="目标 input token 数（真实 summarizer 形状：~5-7k）")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=400)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "artifacts" / "phase4a" / "micro")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    base_url, api_key = _local_endpoint()
    if not base_url:
        print("[FAIL] config 中未找到本地 base_url", file=sys.stderr)
        return 3
    url = base_url + "/chat/completions"

    count_tokens = _make_token_counter()
    results = []
    for size in args.sizes:
        prompt, actual = _prompt_of_tokens(size, count_tokens)
        for rep in range(1, args.reps + 1):
            before = _snapshot()
            t0 = time.time()
            resp = _post(url, {
                "model": "qwen3-30b-a3b-local",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": args.max_tokens,
                "temperature": 0.0,
                "chat_template_kwargs": {"enable_thinking": False},
            }, api_key)
            latency = time.time() - t0
            after = _snapshot()

            usage = resp.get("usage") or {}
            timing = {}
            if before and after:
                for name, label in (
                    ("vllm:request_queue_time_seconds", "queue"),
                    ("vllm:request_prefill_time_seconds", "prefill"),
                    ("vllm:request_decode_time_seconds", "decode"),
                    ("vllm:time_to_first_token_seconds", "ttft"),
                ):
                    d = delta_histogram(before["histograms"].get(name), after["histograms"].get(name))
                    timing[label] = {
                        "p50": histogram_quantile(d, 0.5),
                        "value": next(iter(d["buckets"].values())) if d and d.get("buckets") else None,
                    }
            record = {
                "target_tokens": size,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "latency_s": round(latency, 3),
                "timing": timing,
                "rep": rep,
            }
            results.append(record)
            print(f"  size={size:>5} rep{rep} latency={latency:5.2f}s "
                  f"prompt={record['prompt_tokens']} gen={record['completion_tokens']} timing={json.dumps(timing, default=str)[:120]}")

    payload = {"endpoint": url, "max_tokens": args.max_tokens, "results": results}
    (args.out / "micro_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"-> {args.out / 'micro_results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
