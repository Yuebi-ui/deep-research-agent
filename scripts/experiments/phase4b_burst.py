#!/usr/bin/env python
"""Phase 4B：claim_verification burst 的固定 workload + 受控并发 sweep。

    # 1) 生成/校验冻结的 workload manifest（用 vLLM /tokenize 精确拟合形状）
    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/phase4b_burst.py \
        --build-manifest --out-dir artifacts/phase4b

    # 2) Stage 1：4/6/8/10/12 各 1 次
    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/phase4b_burst.py \
        --concurrency 4 6 8 10 12 --rep 1 --out-dir artifacts/phase4b

    # 3) Stage 2：Top-2 追加 2 次
    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/experiments/phase4b_burst.py \
        --concurrency 8 10 --rep 2 3 --out-dir artifacts/phase4b

只读约束：不发写请求、不写 task DB / memory、不改 vLLM 配置；只发本地 vLLM 的
chat/completions 与 /metrics 读取。prompt 为确定性合成内容（无真实网页数据）。

每个测量点前会先跑一次**丢弃的 warmup burst**（同 workload、同并发）：本地
vLLM 没有 /reset_prefix_cache 端点，用同 workload 预填可以让各档进入测量时
的 prefix cache 状态一致，避免"后跑的档位白捡命中"。

    ALLOW_LIVE_EXTERNAL_APIS=true 仅用于放行 127.0.0.1 的本地调用（不产生云费用）。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from deep_research.profiling.burst import burst_stats, run_burst  # noqa: E402
from deep_research.profiling.sampler import ProfilerSampler, fetch_metrics_text  # noqa: E402
from deep_research.profiling.scrape import (  # noqa: E402
    delta_histogram,
    extract_snapshot,
    histogram_mean,
    histogram_quantile,
)
from deep_research.profiling.workload import (  # noqa: E402
    build_manifest,
    load_manifest,
    manifest_fingerprint,
    verify_manifest,
    write_manifest,
)

METRICS_URL = "http://127.0.0.1:8001/metrics"


# ===== 配置（与生产同一份 role 配置，保证请求形状一致）=====


def _local_role_cfg() -> dict:
    from deep_research.settings import get_engine_settings
    from deep_research.utils import load_config

    cfg = load_config(stage_name=get_engine_settings().stage)
    cognition = (cfg.get("cognition") or {})
    roles = cfg.get("roles") or {}
    role = roles.get("researcher_summarizer") or {}
    api = cognition.get(role.get("backend") or "openai_local") or {}
    return {
        "base_url": str(api.get("base_url") or "").rstrip("/"),
        "api_key": str(api.get("api_key") or ""),
        "model": str(role.get("handle") or ""),
        "extra_body": dict(api.get("extra_body") or {}),
    }


def _make_token_counter(role_cfg: dict):
    base_url = role_cfg["base_url"]
    root = base_url[:-3] if base_url.endswith("/v1") else base_url

    def _count(text: str) -> int | None:
        try:
            resp = _post_json(
                root + "/tokenize",
                {"model": role_cfg["model"], "prompt": text},
                role_cfg["api_key"],
                timeout=60,
            )
            count = resp.get("count")
            if count is None:
                count = len(resp.get("tokens") or [])
            return int(count)
        except Exception:
            return None

    if _count("ping") is None:
        raise RuntimeError("vLLM /tokenize 不可用：manifest 需要精确 token 计数")
    return _count


# ===== HTTP =====


def _post_json(url: str, payload: dict, api_key: str, timeout: float = 600.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _make_sender(role_cfg: dict, url: str):
    """构造与生产同形状的 sender（同一个 base_url / model / extra_body）。

    采样参数**与生产一致**：不传 temperature（用模型自带 generation config）。
    早期尝试过 temperature=0 求确定性，但实测两点否掉了它：
    1) 贪心 + 合成正文会周期性陷入重复循环 → 打满 max_tokens 截断 → JSON 不可解析；
    2) vLLM 连续批处理下贪心输出本身不可复现（批形状不同 → 数值路径不同），
       "确定性"并不成立。
    与生产的**唯一**差异是显式 max_tokens（生产不传、由模型自然停止），
    作为固定工作量的上界；是否触顶会被显式记录为 workload 有效性信号。
    """

    def send(spec: dict) -> dict:
        payload = {
            "model": role_cfg["model"],
            "messages": [{"role": "user", "content": spec["prompt"]}],
            "max_tokens": int(spec.get("max_tokens") or 600),
        }
        # 生产 backend 级 extra_body（含 chat_template_kwargs.enable_thinking=false）
        for key, value in (role_cfg.get("extra_body") or {}).items():
            payload.setdefault(key, value)
        try:
            resp = _post_json(url, payload, role_cfg["api_key"])
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:400]
            return {"ok": False, "status": exc.code, "content": None, "usage": None, "error": body}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": None, "content": None, "usage": None,
                    "error": f"{type(exc).__name__}: {exc}"}
        choices = resp.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        return {"ok": True, "status": 200, "content": content, "usage": resp.get("usage"),
                "finish_reason": choices[0].get("finish_reason") if choices else None, "error": None}

    return send


def _snapshot() -> dict | None:
    text = fetch_metrics_text(METRICS_URL)
    return extract_snapshot(text) if text else None


def _wait_idle(*, timeout_s: float = 120.0, need_consecutive: int = 3, interval: float = 1.0) -> dict:
    """等待 vLLM 完全空闲（没有其它流量混进测量窗口）。"""
    consec = 0
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        snap = _snapshot()
        if snap:
            running = (snap["scalars"].get("vllm:num_requests_running") or 0.0)
            waiting = (snap["scalars"].get("vllm:num_requests_waiting") or 0.0)
            kv = (snap["scalars"].get("vllm:kv_cache_usage_perc") or 0.0)
            last = {"running": running, "waiting": waiting, "kv": kv}
            if running == 0 and waiting == 0 and kv == 0:
                consec += 1
                if consec >= need_consecutive:
                    return {"idle": True, "waited_s": round(timeout_s - (deadline - time.time()), 1), **last}
            else:
                consec = 0
        time.sleep(interval)
    return {"idle": False, **last}


# ===== 单次 burst =====


def _hist_summary(before: dict | None, after: dict | None, name: str, label: str) -> dict:
    if not before or not after:
        return {"n": 0, "p50": None, "p90": None, "mean": None}
    delta = delta_histogram(before["histograms"].get(name), after["histograms"].get(name))
    count = (delta or {}).get("count")
    return {
        "n": int(count) if count else 0,
        "p50": histogram_quantile(delta, 0.5),
        "p90": histogram_quantile(delta, 0.9),
        "mean": histogram_mean(delta),
    }


def _counter_delta(before: dict | None, after: dict | None, name: str) -> float | None:
    if not before or not after:
        return None
    a = before["scalars"].get(name)
    b = after["scalars"].get(name)
    return None if a is None or b is None else b - a


def run_point(*, manifest: dict, concurrency: int, rep: int, out_dir: Path,
              role_cfg: dict, do_warmup: bool, interval: float) -> dict:
    requests = manifest["requests"]
    sender = _make_sender(role_cfg, role_cfg["base_url"] + "/chat/completions")
    point_dir = out_dir / "sweep" / f"c{concurrency}" / f"rep{rep}"
    point_dir.mkdir(parents=True, exist_ok=True)

    idle = _wait_idle()
    if not idle.get("idle"):
        raise RuntimeError(f"vLLM 未在超时内空闲（可能有其它任务在跑）：{idle}")

    if do_warmup:
        # 丢弃：只为把 prefix cache 预热成"与本档一致"的状态
        import asyncio
        warm = asyncio.run(run_burst(requests, concurrency=concurrency, send_fn=sender))
        warm_summary = {"wall_s": warm["wall_s"], "failures": sum(1 for r in warm["results"] if not r.get("ok"))}
        time.sleep(1.0)
    else:
        warm_summary = None

    before = _snapshot()
    if before is None:
        raise RuntimeError("burst 前无法读取 /metrics（profiler 不可信，按 STOP 处理）")

    sampler = ProfilerSampler(point_dir, interval=interval, vllm_metrics_url=METRICS_URL)
    sampler_thread = threading.Thread(target=sampler.run, daemon=True)
    sampler_thread.start()

    import asyncio
    t_wall_start = time.time()
    burst = asyncio.run(run_burst(requests, concurrency=concurrency, send_fn=sender))
    wall_epoch_end = time.time()
    time.sleep(0.5)
    sampler.request_stop()
    sampler_thread.join(timeout=10)

    after = _snapshot()
    stats = burst_stats(burst)
    stats.update({
        "rep": rep,
        "manifest_fingerprint": manifest.get("fingerprint"),
        "workload_fingerprint_recomputed": manifest_fingerprint(requests),
        "request_prompt_sha1s": [r["prompt_sha1"] for r in burst["results"]],
        "sampling": {"temperature": "provider_default (未发送)",
                     "max_tokens": manifest.get("max_tokens"),
                     "note": "与生产一致地不传 temperature；仅显式 max_tokens 作为工作量上界"},
        "warmup": warm_summary,
        "idle_check": idle,
        "wall_epoch_start": t_wall_start,
        "wall_epoch_end": wall_epoch_end,
        "vllm": {
            "queue": _hist_summary(before, after, "vllm:request_queue_time_seconds", "queue"),
            "ttft": _hist_summary(before, after, "vllm:time_to_first_token_seconds", "ttft"),
            "prefill": _hist_summary(before, after, "vllm:request_prefill_time_seconds", "prefill"),
            "decode": _hist_summary(before, after, "vllm:request_decode_time_seconds", "decode"),
            "e2e": _hist_summary(before, after, "vllm:e2e_request_latency_seconds", "e2e"),
            "counters": {
                "prompt_tokens": _counter_delta(before, after, "vllm:prompt_tokens_total"),
                "generation_tokens": _counter_delta(before, after, "vllm:generation_tokens_total"),
                "preemptions": _counter_delta(before, after, "vllm:num_preemptions_total"),
                "request_success": _counter_delta(before, after, "vllm:request_success_total"),
                "prefix_cache_hits": _counter_delta(before, after, "vllm:prefix_cache_hits_total"),
                "prefix_cache_queries": _counter_delta(before, after, "vllm:prefix_cache_queries_total"),
                "prompt_tokens_cached": _counter_delta(before, after, "vllm:prompt_tokens_cached_total"),
            },
            "missing_metrics": sorted(
                name for name in ("vllm:num_requests_running", "vllm:kv_cache_usage_perc")
                if before["scalars"].get(name) is None
            ),
        },
        "sampler": {"samples": sampler.samples, "errors": dict(sampler.errors)},
    })

    gpu = _read_gpu_csv(point_dir / "gpu_metrics.csv")
    vllm = _read_vllm_csv(point_dir / "vllm_metrics.csv")
    stats["gauges"] = _gauge_summary(gpu, vllm)
    stats["hard_gates_pass"], stats["hard_gate_failures"] = _hard_gates(stats)

    (point_dir / "burst_results.json").write_text(
        json.dumps(burst["results"], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (point_dir / "summary.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return stats


def _read_gpu_csv(path: Path) -> list[dict]:
    import csv

    if not path.exists():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({k: (float(v) if v not in ("", None) else None) for k, v in row.items()})
            except ValueError:
                continue
    return rows


def _read_vllm_csv(path: Path) -> list[dict]:
    return _read_gpu_csv(path)


def _gauge_summary(gpu: list[dict], vllm: list[dict]) -> dict:
    def _vals(rows, key):
        return [r[key] for r in rows if isinstance(r.get(key), (int, float))]

    gpu_util = _vals(gpu, "gpu_util_percent")
    kv = _vals(vllm, "kv_cache_usage_perc")
    return {
        "samples_gpu": len(gpu), "samples_vllm": len(vllm),
        "gpu_util_mean": (sum(gpu_util) / len(gpu_util)) if gpu_util else None,
        "gpu_util_max": max(gpu_util) if gpu_util else None,
        "gpu_busy_gt50_samples": sum(1 for v in gpu_util if v > 50),
        "kv_peak": max(kv) if kv else None,
        "running_peak": max(_vals(vllm, "num_requests_running")) if _vals(vllm, "num_requests_running") else None,
        "waiting_peak": max(_vals(vllm, "num_requests_waiting")) if _vals(vllm, "num_requests_waiting") else None,
    }


def _hard_gates(stats: dict) -> tuple[bool, list[str]]:
    fails: list[str] = []
    if stats["failures"]:
        fails.append(f"request_failures={stats['failures']} {stats['failure_kinds']}")
    if stats["incorrect"]:
        fails.append(f"correctness_failures={stats['incorrect']} {stats['defect_counts']}")
    if stats["failure_kinds"].get("oom"):
        fails.append("oom")
    if stats["failure_kinds"].get("context_overflow"):
        fails.append("context_overflow")
    if stats.get("truncated"):
        # 打满 max_tokens = workload 形状失真（生产里模型是自然停止的）
        fails.append(f"truncated_responses={stats['truncated']}")
    if stats["vllm"]["counters"].get("preemptions") is None:
        fails.append("vllm_metrics_missing")
    if sum((stats["sampler"]["errors"] or {}).values()):
        fails.append(f"sampler_errors={stats['sampler']['errors']}")
    if stats["vllm"]["missing_metrics"]:
        fails.append(f"missing_metrics={stats['vllm']['missing_metrics']}")
    return (not fails), fails


# ===== main =====


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "artifacts" / "phase4b")
    parser.add_argument("--manifest", type=Path, default=None, help="默认 <out-dir>/workload_manifest.json")
    parser.add_argument("--build-manifest", action="store_true", help="生成/刷新 manifest 后退出")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[])
    parser.add_argument("--rep", nargs="+", type=int, default=[1])
    parser.add_argument("--interval", type=float, default=0.5, help="采样间隔（burst 短，0.5s 提高 KV 峰值分辨率）")
    parser.add_argument("--no-warmup", action="store_true", help="跳过测量前的丢弃 warmup（会引入 prefix cache 干扰）")
    args = parser.parse_args(argv)

    manifest_path = args.manifest or (args.out_dir / "workload_manifest.json")
    role_cfg = _local_role_cfg()
    if not role_cfg["base_url"]:
        print("[FAIL] config 中未找到 openai_local base_url", file=sys.stderr)
        return 3

    if args.build_manifest or not manifest_path.exists():
        counter = _make_token_counter(role_cfg)
        manifest = build_manifest(counter)
        write_manifest(manifest_path, manifest)
        print(f"[manifest] {manifest_path} fingerprint={manifest['fingerprint']} "
              f"requests={len(manifest['requests'])} tokens={sum(r['prompt_tokens'] for r in manifest['requests'])}")
    else:
        manifest = load_manifest(manifest_path)

    problems = verify_manifest(manifest)
    if problems:
        print(f"[FAIL] manifest 校验失败：{problems}", file=sys.stderr)
        return 4
    print(f"[manifest] fingerprint={manifest['fingerprint']} requests={len(manifest['requests'])} "
          f"tokens={sum(r['prompt_tokens'] for r in manifest['requests'])}")

    if args.build_manifest and not args.concurrency:
        return 0
    if not args.concurrency:
        print("[FAIL] 需要 --concurrency（或 --build-manifest）", file=sys.stderr)
        return 2

    code = 0
    for concurrency in args.concurrency:
        for rep in args.rep:
            print(f"[burst] c={concurrency} rep={rep} ...", flush=True)
            try:
                stats = run_point(manifest=manifest, concurrency=concurrency, rep=rep,
                                  out_dir=args.out_dir, role_cfg=role_cfg,
                                  do_warmup=not args.no_warmup, interval=args.interval)
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] c={concurrency} rep={rep}: {type(exc).__name__}: {exc}", file=sys.stderr)
                code = 5
                continue
            print(f"    wall={stats['wall_s']:.1f}s lat_p50={stats['latency_s']['p50']} "
                  f"lat_p90={stats['latency_s']['p90']} kv_peak={stats['gauges']['kv_peak']} "
                  f"wait_peak={stats['gauges']['waiting_peak']} preempt={stats['vllm']['counters']['preemptions']} "
                  f"gates={'PASS' if stats['hard_gates_pass'] else stats['hard_gate_failures']}", flush=True)
            if not stats["hard_gates_pass"]:
                code = code or 6
    return code


if __name__ == "__main__":
    sys.exit(main())
