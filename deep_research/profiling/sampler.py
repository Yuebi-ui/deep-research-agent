"""Phase 4A 采样器：按固定间隔抓取 GPU 与 vLLM metrics（只读）。

    .venv/bin/python -m deep_research.profiling.sampler --out-dir artifacts/phase4a/<run_id> --interval 1.0

设计约束：

- **只读**：仅 HTTP GET /metrics 与 nvidia-smi 查询，不接触业务状态；
- **故障隔离**：任一数据源失败 → 该行字段留空并计数，**绝不抛出**到调用方；
- **即时落盘**：每行 append+flush，进程被杀也不丢已采数据；
- 每行只含数值与时间戳，不含任何请求/响应文本。
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import threading
import time
import urllib.request
from pathlib import Path

from deep_research.profiling.gpu import query_gpu
from deep_research.profiling.scrape import SCALAR_METRICS, extract_snapshot

DEFAULT_VLLM_METRICS_URL = "http://127.0.0.1:8001/metrics"

GPU_COLUMNS = ("gpu_util_percent", "mem_util_percent", "mem_used_mib", "mem_total_mib", "power_w", "temp_c")
# 标量列名：去掉 vllm: 前缀（CSV 友好）
SCALAR_COLUMNS = tuple(name.split(":", 1)[1] for name in SCALAR_METRICS)


def _short_name(metric: str) -> str:
    return metric.split(":", 1)[1]


def fetch_metrics_text(url: str, timeout: float = 3.0) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


class ProfilerSampler:
    def __init__(
        self,
        out_dir: Path,
        *,
        interval: float = 1.0,
        vllm_metrics_url: str = DEFAULT_VLLM_METRICS_URL,
        gpu_fn=query_gpu,
        fetch_fn=fetch_metrics_text,
        now=time.time,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.interval = interval
        self.vllm_metrics_url = vllm_metrics_url
        self._gpu_fn = gpu_fn
        self._fetch_fn = fetch_fn
        self._now = now
        self._stop = threading.Event()
        self.errors = {"gpu": 0, "vllm": 0}
        self.samples = 0

    # ---- 单次采样（纯函数化，便于测试）----

    def sample_once(self) -> dict:
        ts = self._now()
        gpu = None
        try:
            gpu = self._gpu_fn()
        except Exception:
            gpu = None
        if gpu is None:
            self.errors["gpu"] += 1

        scalars: dict[str, float | None] = {}
        histograms: dict = {}
        text = None
        try:
            text = self._fetch_fn(self.vllm_metrics_url)
        except Exception:
            text = None
        if text:
            snapshot = extract_snapshot(text)
            scalars = snapshot["scalars"]
            histograms = snapshot["histograms"]
        else:
            self.errors["vllm"] += 1

        return {"ts": ts, "gpu": gpu or {}, "scalars": scalars, "histograms": histograms}

    # ---- 落盘 ----

    def _open_sinks(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._gpu_f = open(self.out_dir / "gpu_metrics.csv", "w", newline="", encoding="utf-8")
        self._vllm_f = open(self.out_dir / "vllm_metrics.csv", "w", newline="", encoding="utf-8")
        self._hist_f = open(self.out_dir / "vllm_histograms.jsonl", "w", encoding="utf-8")
        self._gpu_w = csv.writer(self._gpu_f)
        self._vllm_w = csv.writer(self._vllm_f)
        self._gpu_w.writerow(("ts", *GPU_COLUMNS))
        self._vllm_w.writerow(("ts", *SCALAR_COLUMNS))

    def _write(self, sample: dict) -> None:
        self._gpu_w.writerow([f"{sample['ts']:.3f}", *[sample["gpu"].get(col) for col in GPU_COLUMNS]])
        self._vllm_w.writerow([f"{sample['ts']:.3f}", *[sample["scalars"].get(name) for name in SCALAR_METRICS]])
        self._hist_f.write(json.dumps({"ts": sample["ts"], "histograms": sample["histograms"]}, default=str) + "\n")
        for f in (self._gpu_f, self._vllm_f, self._hist_f):
            f.flush()

    # ---- 主循环 ----

    def request_stop(self, *_args) -> None:
        self._stop.set()

    def run(self, *, max_seconds: float | None = None) -> int:
        self._open_sinks()
        started = self._now()
        try:
            while not self._stop.is_set():
                if max_seconds is not None and self._now() - started >= max_seconds:
                    break
                tick = self._now()
                try:
                    sample = self.sample_once()
                    self._write(sample)
                    self.samples += 1
                except Exception:
                    # 采样循环绝不允许因单次异常退出（数据源已在 sample_once 内隔离；
                    # 这里是最后一道防线，例如磁盘写入失败）
                    self.errors["write"] = self.errors.get("write", 0) + 1
                elapsed = self._now() - tick
                self._stop.wait(max(0.0, self.interval - elapsed))
        finally:
            for f in (self._gpu_f, self._vllm_f, self._hist_f):
                try:
                    f.close()
                except Exception:
                    pass
            summary = {"samples": self.samples, "errors": self.errors, "interval": self.interval}
            (self.out_dir / "sampler_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4A read-only GPU/vLLM sampler")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--vllm-metrics-url", default=DEFAULT_VLLM_METRICS_URL)
    parser.add_argument("--max-seconds", type=float, default=None, help="安全上限（默认无上限，Ctrl-C/SIGTERM 停止）")
    args = parser.parse_args(argv)

    sampler = ProfilerSampler(args.out_dir, interval=args.interval, vllm_metrics_url=args.vllm_metrics_url)
    signal.signal(signal.SIGTERM, sampler.request_stop)
    signal.signal(signal.SIGINT, sampler.request_stop)
    print(f"[sampler] out={args.out_dir} interval={args.interval}s url={args.vllm_metrics_url}")
    code = sampler.run(max_seconds=args.max_seconds)
    print(f"[sampler] samples={sampler.samples} errors={sampler.errors}")
    return code


if __name__ == "__main__":
    sys.exit(main())
