"""nvidia-smi 只读遥测（Phase 4A）。

用 `nvidia-smi --query-gpu=... --format=csv` 单次调用取全部字段（一个子进程，
~20ms），失败返回 None——采集器不得因 GPU 查询故障影响工作流。
"""

from __future__ import annotations

import subprocess

GPU_FIELDS = (
    "utilization.gpu",
    "utilization.memory",
    "memory.used",
    "memory.total",
    "power.draw",
    "temperature.gpu",
)

_FIELD_NAMES = {
    "utilization.gpu": "gpu_util_percent",
    "utilization.memory": "mem_util_percent",
    "memory.used": "mem_used_mib",
    "memory.total": "mem_total_mib",
    "power.draw": "power_w",
    "temperature.gpu": "temp_c",
}


def _to_float(raw: str) -> float | None:
    raw = raw.strip()
    if not raw or raw.lower() in ("n/a", "[not supported]", "[n/a]"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def query_gpu(timeout: float = 5.0, runner=subprocess.run) -> dict[str, float | None] | None:
    """返回 {gpu_util_percent, mem_util_percent, mem_used_mib, mem_total_mib, power_w, temp_c}。"""
    cmd = [
        "nvidia-smi",
        f"--query-gpu={','.join(GPU_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            return None
        line = proc.stdout.strip().splitlines()[0]
    except Exception:
        return None
    values = [part for part in line.split(",")]
    if len(values) != len(GPU_FIELDS):
        return None
    return {_FIELD_NAMES[field]: _to_float(raw) for field, raw in zip(GPU_FIELDS, values)}
