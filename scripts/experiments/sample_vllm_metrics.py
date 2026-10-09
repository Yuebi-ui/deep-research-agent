#!/usr/bin/env python
"""E7：vLLM /metrics 采样器（纯观测，不调任何参数）。

    .venv/bin/python scripts/experiments/sample_vllm_metrics.py --interval 2 --out /tmp/vllm_metrics.jsonl

每 interval 秒抓取一次 Prometheus 文本并追加一行 JSON（只保留关键序列）：

* num_requests_running / num_requests_waiting   （并发与排队）
* gpu_cache_usage_perc                          （KV cache 占用）
* prompt_tokens_total / generation_tokens_total （吞吐，counter）
* prefix_cache_hits_total / prefix_cache_queries_total
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

_SERIES = [
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:kv_cache_usage_perc",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "vllm:prefix_cache_hits_total",
    "vllm:prefix_cache_queries_total",
    "vllm:request_success_total",
]


def _fetch(url: str) -> str:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310  # 本机服务
        return resp.read().decode("utf-8", errors="replace")


def _parse(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        for series in _SERIES:
            if line.startswith(series + "{") or line.startswith(series + " "):
                try:
                    out[series] = float(line.rsplit(" ", 1)[1])
                except ValueError:
                    pass
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8001/metrics")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=Path("/tmp/vllm_metrics.jsonl"))
    args = parser.parse_args()

    print(f"[sampler] every {args.interval}s -> {args.out}", flush=True)
    with open(args.out, "a", encoding="utf-8") as f:
        while True:
            try:
                values = _parse(_fetch(args.url))
                row = {"ts": time.time(), **values}
                f.write(json.dumps(row) + "\n")
                f.flush()
            except Exception as exc:  # noqa: BLE001  # 采样失败只跳过本轮
                f.write(json.dumps({"ts": time.time(), "error": str(exc)[:120]}) + "\n")
                f.flush()
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
