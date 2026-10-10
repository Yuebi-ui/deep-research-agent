#!/usr/bin/env python
"""V1 Baseline Runner —— 跑一次未经优化的真实 Deep Research 任务并产出机器可读 artifacts。

    .venv/bin/python scripts/run_baseline.py

职责（与本文件之外的系统边界）：

1. 固定 baseline case（默认复用历史 E2E query）
2. 生成唯一 run_id，并通过 ``POST /api/research/start`` 传给 API（写入 Redis）
3. 自动完成 HITL review（默认 approve）
4. 轮询到终态
5. 收集 worker 侧采集的原始指标（``data/baseline_metrics/<run_id>/``）
6. 聚合输出到 ``artifacts/baseline/<run_id>/``：
   run.json / llm_calls.jsonl / node_metrics.jsonl / search_metrics.jsonl /
   reliability_events.jsonl / BASELINE.md
7. 打印简短 summary

本脚本 **不** 重新实现部署：Redis / vLLM / API / Worker 必须先运行。
前置启动入口见 `scripts/autodl/` 与 `scripts/model-service/README.md`。

本脚本不做任何优化、不修改业务行为 —— 只运行与观测。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# ===== 固定 Baseline Case（复用历史真实 E2E 使用的 query）=====

BASELINE_CASE: dict = {
    "case_id": "hybrid-e2e-langgraph-2026-10-05",
    "query": "What is LangGraph and what are its main use cases?",
    "review_action": "approve",
    "variant": "v1_baseline",
}

_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "deleted"}

# 当前 comparison baseline 的本地上下文上限（Phase 2 起固定；不要悄悄放大）
DEFAULT_EXPECTED_CONTEXT_LIMIT = 8192


# ===== HTTP 小工具（stdlib，避免额外依赖）=====


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {body[:500]}") from exc


def _get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} GET {url}: {body[:500]}") from exc


# ===== 环境快照（不含任何密钥）=====


def _run_cmd(cmd: list[str], timeout: float = 10.0) -> str | None:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _package_versions() -> dict:
    from importlib import metadata

    versions: dict[str, str | None] = {"python": sys.version.split()[0]}
    for pkg in ("langchain", "langchain-core", "langgraph", "langchain-openai", "fastapi"):
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            versions[pkg] = None
    return versions


def _local_model_info() -> dict:
    """探测本地 vLLM 的模型 id / max_model_len（读 config.yml 的 base_url）。"""
    info: dict = {"model": None, "max_model_len": None, "base_url": None}
    try:
        from deep_research.utils import load_config
        from deep_research.settings import get_engine_settings

        cfg = load_config(stage_name=get_engine_settings().stage)
        local_cfg = (cfg.get("cognition") or {}).get("openai_local") or {}
        base_url = str(local_cfg.get("base_url") or "")
        api_key = str(local_cfg.get("api_key") or "")
        info["base_url"] = base_url
        if not base_url:
            return info
        req = urllib.request.Request(base_url.rstrip("/") + "/models")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        for item in payload.get("data") or []:
            info["model"] = item.get("id")
            for key in ("max_model_len", "max_context_length"):
                if isinstance(item.get(key), int):
                    info["max_model_len"] = item[key]
                    break
            break
    except Exception as exc:
        info["probe_error"] = str(exc)[:200]
    return info


def _routing_snapshot() -> dict:
    """roles → backend/handle（不含密钥）。"""
    try:
        from deep_research.utils import load_config
        from deep_research.settings import get_engine_settings

        cfg = load_config(stage_name=get_engine_settings().stage)
        roles = cfg.get("roles") or {}
        return {
            role: {"backend": (rc or {}).get("backend"), "handle": (rc or {}).get("handle")}
            for role, rc in roles.items()
        }
    except Exception as exc:
        return {"error": str(exc)[:200]}


def _vllm_version() -> str | None:
    vllm_pip = Path("/root/autodl-tmp/vllm-env/bin/pip")
    if vllm_pip.exists():
        out = _run_cmd([str(vllm_pip), "show", "vllm"], timeout=20)
        if out:
            for line in out.splitlines():
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
    return None


def _memory_doc_count() -> int | None:
    try:
        from deep_research.memory.vector_store import VectorMemoryStore
        from deep_research.settings import get_engine_settings

        store = VectorMemoryStore(
            persist_dir=str(get_engine_settings().resolved_memory_data_dir / "chroma")
        )
        return int(store.count())
    except Exception:
        return None


def collect_environment(api_base: str) -> dict:
    gpu = _run_cmd([
        "nvidia-smi",
        "--query-gpu=name,memory.total,driver_version",
        "--format=csv,noheader",
    ])
    local = _local_model_info()
    health = None
    try:
        health = _get_json(f"{api_base}/api/health").get("status")
    except Exception as exc:
        health = f"unreachable: {str(exc)[:120]}"

    import os

    return {
        "gpu": gpu or "unavailable",
        "local_model": local.get("model") or "unavailable",
        "serving": {
            "backend": "vllm",
            "vllm_version": _vllm_version(),
            "base_url": local.get("base_url"),
        },
        "context_limit_local": local.get("max_model_len"),
        "context_limit_source": "vllm /v1/models probe" if local.get("max_model_len") else "unavailable",
        "routing": _routing_snapshot(),
        "app_env": os.environ.get("APP_ENV", "(unset -> development)"),
        "stage": os.environ.get("STAGE", "(unset -> prod)"),
        "checkpointer_backend": os.environ.get("CHECKPOINTER_BACKEND", "(unset)"),
        "config_path": os.environ.get("CONFIG_PATH", "config.yml"),
        "versions": _package_versions(),
        "api_health": health,
        "memory_store_docs": _memory_doc_count(),
        "snapshot_at": datetime.now().isoformat(timespec="seconds"),
    }


# ===== 任务驱动 =====


def start_task(api_base: str, query: str, run_id: str) -> str:
    created = _post_json(f"{api_base}/api/research/start", {"query": query, "run_id": run_id})
    thread_id = created.get("thread_id")
    if not thread_id:
        raise RuntimeError(f"start 返回异常: {created}")
    return thread_id


def wait_for_terminal(
    api_base: str,
    thread_id: str,
    *,
    review_actions: str | list[str],
    poll_interval: float,
    timeout_seconds: float,
) -> dict:
    """轮询状态；遇到 waiting_review 自动提交 review；返回最终 status dict。

    `review_actions` 支持**序列**（Phase 5B：验证 reject → regenerate → accept）：

    - 字符串 "approve" / "revise"：单调行为，但 `revise` 在投机拓扑下会**再次**
      回到 waiting_review —— 若同一 action 一直重复提交会形成 revise 死循环；
    - 逗号序列（如 "revise,approve"）：按序消费，末尾动作对后续每次等待重复。
      例：`revise,approve` = 第一次 revise（触发最坏路径研究重跑）→ 之后 approve。
    """
    actions = (
        [a.strip() for a in review_actions.split(",") if a.strip()]
        if isinstance(review_actions, str)
        else list(review_actions)
    ) or ["approve"]

    deadline = time.time() + timeout_seconds
    review_submitted = False
    review_count = 0
    last_status = ""
    while time.time() < deadline:
        status = _get_json(f"{api_base}/api/research/{thread_id}/status")
        current = str(status.get("status", "unknown"))
        if current != last_status:
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] status={current} stage={status.get('stage', '')}")
            last_status = current

        if current == "waiting_review" and not review_submitted:
            action = actions[min(review_count, len(actions) - 1)]
            _post_json(
                f"{api_base}/api/research/{thread_id}/resume",
                {"action": action, "feedback": "phase5b automatic rejection" if action == "revise" else ""},
            )
            review_submitted = True
            review_count += 1
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] 已提交 review #{review_count} action={action}")

        if current in _TERMINAL_STATUSES:
            status["review_count"] = review_count
            return status
        if current != "waiting_review":
            review_submitted = False
        time.sleep(poll_interval)

    status = _get_json(f"{api_base}/api/research/{thread_id}/status")
    status["runner_timeout"] = True
    status["review_count"] = review_count
    return status


# ===== 采集与聚合 =====


def read_task_row(thread_id: str) -> dict:
    from backend.db.engine import resolve_database_path

    db_path = resolve_database_path()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _collect_rows(run_id: str, task_id: str) -> tuple[dict[str, list[dict]], list[str]]:
    """从 data/baseline_metrics/ 收集本 task 的行（按 run_id 目录，按 task_id 兜底）。"""
    from deep_research.callbacks import baseline_metrics as bm
    from deep_research.callbacks import baseline_report as br

    root = bm.default_raw_root()
    rows: dict[str, list[dict]] = {k: [] for k in
                                   ("llm_calls", "node_metrics", "search_metrics",
                                    "budget_events", "reliability_events")}
    sources: list[str] = []

    if not root.exists():
        return rows, sources

    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        candidate = br.load_rows(directory)
        candidate_rows = (
            candidate["llm_calls"] + candidate["node_metrics"]
            + candidate["search_metrics"] + candidate["budget_events"]
            + candidate["reliability_events"]
        )
        belongs = directory.name == bm.safe_run_id(run_id) or any(
            r.get("task_id") == task_id for r in candidate_rows
        )
        if not belongs:
            continue
        sources.append(directory.name)
        for kind in rows:
            for r in candidate[kind]:
                if r.get("task_id") == task_id:
                    rows[kind].append(r)

    for kind in rows:
        rows[kind].sort(key=lambda r: r.get("started_at") or r.get("timestamp") or 0)
    return rows, sources


def write_artifacts(
    out_dir: Path,
    run_id: str,
    task: dict,
    rows: dict[str, list[dict]],
    environment: dict,
    case: dict,
    sources: list[str],
    extra: dict | None = None,
) -> dict:
    from deep_research.callbacks import baseline_report as br

    out_dir.mkdir(parents=True, exist_ok=True)
    run_json = br.aggregate(
        rows, run_id=run_id, task=task, environment=environment, case=case,
        generated_extra=extra,
    )
    if sources and sources != [run_id]:
        run_json["data_quality"]["notes"].append(
            f"原始指标来源目录: {sources}（与 run_id 不完全一致，可能含 auto- 回退）"
        )

    (out_dir / "run.json").write_text(
        json.dumps(run_json, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    for kind, filename in (
        ("llm_calls", "llm_calls.jsonl"),
        ("node_metrics", "node_metrics.jsonl"),
        ("search_metrics", "search_metrics.jsonl"),
        ("budget_events", "budget_events.jsonl"),
        ("reliability_events", "reliability_events.jsonl"),
    ):
        with open(out_dir / filename, "w", encoding="utf-8") as f:
            for r in rows[kind]:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    (out_dir / "BASELINE.md").write_text(
        br.render_baseline_md(run_json) + "\n", encoding="utf-8"
    )
    return run_json


def print_summary(run: dict) -> None:
    llm, local, cloud = run["llm"], run["local"], run["cloud"]
    ctx, search, rel = run["context"], run["search"], run["reliability"]
    print()
    print("=" * 62)
    print(f"run_id   : {run['run_id']}")
    print(f"task_id  : {run['task_id']}   status={run['status']}  attempts={run['attempts']}")
    if run["timeline"]["total_latency_ms"]:
        print(f"latency  : {run['timeline']['total_latency_ms'] / 1000:.1f}s")
    print(f"llm      : calls={llm['calls']} success={llm['success']} failed={llm['failed']} "
          f"in={llm['input_tokens']} out={llm['output_tokens']}")
    print(f"local    : calls={local['calls']} overflow={local['context_overflow']}")
    print(f"cloud    : calls={cloud['calls']} est_cost={cloud['estimated_cost_rmb']} RMB")
    print(f"overflow : count={ctx['overflow_count']} rate_local={ctx['overflow_rate_of_local_calls']} "
          f"by_node={ctx['overflow_by_node']}")
    print(f"search   : calls={search['calls']} results={search['results']}")
    print(f"reliab   : claim_losses={rel['claim_losses']} recoveries={rel['recoveries']}")
    print("=" * 62)


# ===== Phase 3C P1：run fingerprint / preflight =====


# 当前 accepted thinking 策略（E1a KEEP / E1b REJECT / E8 KEEP / E9 KEEP）
_DEFAULT_THINKING_EXPECTATIONS = {
    "claim_verify": False, "supervisor": True, "writer": False, "draft": False,
}


def _parse_thinking_expectations(args: argparse.Namespace) -> dict[str, bool]:
    """合并默认策略、兼容别名与 --expect-thinking KEY=on|off（后者优先级最高）。"""
    from deep_research.benchmark.fingerprint import THINKING_ENV_KEYS

    expected = dict(_DEFAULT_THINKING_EXPECTATIONS)
    if args.expect_claim_verify_thinking is not None:
        expected["claim_verify"] = args.expect_claim_verify_thinking == "on"
    if args.expect_supervisor_thinking is not None:
        expected["supervisor"] = args.expect_supervisor_thinking == "on"
    for item in args.expect_thinking:
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip().lower()
        if not sep or key not in THINKING_ENV_KEYS:
            raise ValueError(
                f"--expect-thinking 需为 KEY=on|off，KEY ∈ {sorted(THINKING_ENV_KEYS)}，得到 {item!r}"
            )
        if value not in ("on", "off"):
            raise ValueError(f"--expect-thinking {key} 的值必须是 on/off，得到 {value!r}")
        expected[key] = value == "on"
    return expected


def _effective_env(worker_process: dict | None) -> tuple[dict, str]:
    """指纹用的环境：worker 进程环境优先（它才是真正执行 graph 的进程）。

    runner shell 通常没有加载 .env.server，只看 runner 环境会把
    DR_STREAM_USAGE / CHECKPOINTER_BACKEND 等 benchmark 相关开关记成 null。
    """
    import os as _os

    env = dict(_os.environ)
    source = "runner_env"
    worker_env = (worker_process or {}).get("env")
    if isinstance(worker_env, dict):
        env.update(worker_env)
        source = "worker_env+runner_env"
    return env, source


def _build_config_snapshot(probes) -> dict:
    """构造 config 快照；``probes`` 必须是 probe 对象（自带 .worker_process()）。

    刻意要求 probe 对象而不是"进程信息 dict"：preflight 报告里的 worker
    observation 为了防泄漏**剥离了 env 值**，若把它传进来会静默退化成
    runner_env，导致 A/B 两个 variant 的 config fingerprint 完全相同
    （审计链断裂、冻结检测失效）。传 probe 对象可以让这个错误在类型上不可能发生。
    """
    from deep_research.benchmark.fingerprint import build_config_snapshot
    from deep_research.context_budget import (
        DEFAULT_RESERVE_OUTPUT_TOKENS,
        DEFAULT_SAFETY_MARGIN_TOKENS,
    )
    from deep_research.settings import get_engine_settings
    from deep_research.utils import load_config

    stage_cfg = load_config(stage_name=get_engine_settings().stage) or {}
    env, source = _effective_env(probes.worker_process() if probes is not None else None)
    snapshot = build_config_snapshot(
        stage_cfg,
        env=env,
        context_budget_defaults={
            "reserve_output_tokens": DEFAULT_RESERVE_OUTPUT_TOKENS,
            "safety_margin_tokens": DEFAULT_SAFETY_MARGIN_TOKENS,
        },
    )
    snapshot["feature_flags_source"] = source
    return snapshot


def _expected_local_model() -> str | None:
    """预期本地模型 = config roles 里 researcher_main 的 handle。"""
    try:
        from deep_research.settings import get_engine_settings
        from deep_research.utils import load_config

        cfg = load_config(stage_name=get_engine_settings().stage)
        role = (cfg.get("roles") or {}).get("researcher_main") or {}
        handle = role.get("handle")
        return str(handle) if handle else None
    except Exception:
        return None


def _write_preflight_report(run_id: str, payload: dict) -> Path:
    out_dir = REPO_ROOT / "artifacts" / "benchmark_preflight"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.json"
    payload = dict(payload)
    payload["run_id"] = run_id
    payload["checked_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return path


# ===== main =====


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V1 Baseline Runner (Phase 3C P1: fingerprint + preflight)")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--query", default=BASELINE_CASE["query"])
    parser.add_argument("--review-action", default=BASELINE_CASE["review_action"],
                        help="approve | revise | 逗号序列（如 revise,approve —— Phase 5B "
                             "reject→regenerate→accept 验证；末尾动作对后续等待重复）")
    parser.add_argument("--run-id", default=None,
                        help="默认 v1baseline-<UTC时间戳>-<4位随机>")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "artifacts" / "baseline")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=2700.0, help="等待终态的秒数")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 artifacts 目录")

    # ---- experiment identity（7.4）----
    parser.add_argument("--experiment-id", default="adhoc", help="如 writer-thinking-001")
    parser.add_argument("--variant", default=BASELINE_CASE["variant"], help="如 thinking-on")
    parser.add_argument("--experiment-kind", default="baseline",
                        choices=["baseline", "experiment", "smoke"])

    # ---- preflight 预期（7.3：与预期不符 → FAIL FAST）----
    parser.add_argument("--expect-local-model", default=None,
                        help="默认取 config roles.researcher_main.handle")
    parser.add_argument("--expect-context-limit", type=int, default=None,
                        help=f"默认 DR_BASELINE_LOCAL_CONTEXT_LIMIT 或 {DEFAULT_EXPECTED_CONTEXT_LIMIT}")
    parser.add_argument("--expect-thinking", action="append", default=[], metavar="KEY=on|off",
                        help="可重复；KEY ∈ claim_verify|supervisor|writer|draft。"
                             "默认 claim_verify=off / supervisor=on / writer=off / draft=off（当前策略）")
    parser.add_argument("--expect-claim-verify-thinking", choices=["on", "off"], default=None,
                        help="兼容别名（等价 --expect-thinking claim_verify=…）")
    parser.add_argument("--expect-supervisor-thinking", choices=["on", "off"], default=None,
                        help="兼容别名（等价 --expect-thinking supervisor=…）")
    parser.add_argument("--expect-embedding-provider", default="dashscope")
    parser.add_argument("--expect-embedding-model", default="text-embedding-v4")
    parser.add_argument("--expect-embedding-dimension", type=int, default=1024)
    parser.add_argument("--expect-embedding-schema-version", type=int, default=1)
    parser.add_argument("--allow-stale-services", action="store_true",
                        help="允许 worker/API 进程早于最新源码（默认拒绝，防旧配置空跑）")
    args = parser.parse_args(argv)

    run_id = args.run_id or (
        "v1baseline-" + datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    )
    out_dir = args.out_dir / run_id
    if out_dir.exists() and not args.force:
        print(f"[FAIL] artifacts 目录已存在: {out_dir}（用 --force 覆盖）", file=sys.stderr)
        return 3

    # 0. experiment identity + code revision + config fingerprint（7.1/7.2/7.4）
    from deep_research.benchmark.fingerprint import (
        ExperimentIdentity,
        collect_code_revision,
        config_fingerprint,
        evaluate_run_validity,
    )
    from deep_research.benchmark.preflight import Expectations, SystemProbes, run_preflight

    try:
        experiment = ExperimentIdentity(
            experiment_id=args.experiment_id, variant=args.variant, kind=args.experiment_kind
        )
    except ValueError as exc:
        print(f"[FAIL] experiment identity 非法: {exc}", file=sys.stderr)
        return 3

    revision_start = collect_code_revision(REPO_ROOT)

    print(f"[baseline] run_id = {run_id}")
    print(f"[baseline] case   = {BASELINE_CASE['case_id']}")
    print(f"[baseline] query  = {args.query}")
    print(f"[baseline] experiment = {experiment.to_dict()}")
    dirty = "dirty" if revision_start.get("dirty") else "clean"
    print(f"[baseline] revision   = {(revision_start.get('commit') or 'no-git')[:12]} ({dirty}), "
          f"source_fp={revision_start['source_fingerprint'][:16]}…")

    # 0.5 preflight（7.3）：不通过就不启动任务
    context_limit = args.expect_context_limit
    if context_limit is None:
        import os as _os

        context_limit = int(_os.environ.get("DR_BASELINE_LOCAL_CONTEXT_LIMIT", DEFAULT_EXPECTED_CONTEXT_LIMIT))
    try:
        expected_thinking = _parse_thinking_expectations(args)
    except ValueError as exc:
        print(f"[FAIL] thinking 期望参数非法: {exc}", file=sys.stderr)
        return 3
    expectations = Expectations(
        local_model=args.expect_local_model or _expected_local_model(),
        context_limit=context_limit,
        thinking=expected_thinking,
        embedding={
            "provider": args.expect_embedding_provider,
            "model": args.expect_embedding_model,
            "dimension": args.expect_embedding_dimension,
            "schema_version": args.expect_embedding_schema_version,
        },
        allow_stale_services=args.allow_stale_services,
        memory_preset=(args.variant if args.variant in json.loads(
            (REPO_ROOT / "benchmarks/configs/runtime_ablation.v1.json").read_text(encoding="utf-8")
        )["variants"] else None),
    )
    print("[baseline] preflight self-check ...")
    probes = SystemProbes(api_base=args.api_base, repo_root=REPO_ROOT, run_dir=REPO_ROOT / ".run")
    preflight = run_preflight(expectations, probes)
    for check in preflight["checks"]:
        mark = "ok " if check["ok"] else "FAIL"
        print(f"  [{mark}] {check['name']:<20} {check['detail']}")

    # config fingerprint 在 preflight 之后取：feature flags 以 worker 进程环境为准
    config_snapshot = _build_config_snapshot(probes)
    config_fp_start = config_fingerprint(config_snapshot)
    print(f"[baseline] config_fp  = {config_fp_start[:32]}… "
          f"(flags from {config_snapshot.get('feature_flags_source')})")

    report_path = _write_preflight_report(run_id, {
        "preflight": preflight,
        "experiment": experiment.to_dict(),
        "revision": revision_start,
        "config_fingerprint": config_fp_start,
        "config_snapshot": config_snapshot,
    })
    print(f"[baseline] preflight report -> {report_path}")
    if not preflight["passed"]:
        print(f"[FAIL] preflight 未通过: {preflight['failed']}；拒绝启动 benchmark。", file=sys.stderr)
        return 4

    # 1. 环境快照（跑之前采集，避免受本次运行影响）
    print("[baseline] 采集环境快照 ...")
    environment = collect_environment(args.api_base)
    environment["experiment"] = experiment.to_dict()

    # 2. 建任务（run_id 随请求写入 Redis，worker 端贯通）
    try:
        thread_id = start_task(args.api_base, args.query, run_id)
    except Exception as exc:
        print(f"[FAIL] 无法创建任务: {exc}", file=sys.stderr)
        return 3
    print(f"[baseline] task_id = {thread_id}")
    started_at = time.time()

    # 3. 等终态（自动处理 HITL）
    try:
        final_status = wait_for_terminal(
            args.api_base, thread_id,
            review_actions=args.review_action,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:
        print(f"[FAIL] 轮询失败: {exc}", file=sys.stderr)
        return 3

    # 4. 给 worker 侧指标落盘留出余量（所有写入都是同步 append，这里是保险）
    time.sleep(2.0)

    # 5. freeze 检测（7.5）：run 期间代码/配置不得变化
    revision_end = collect_code_revision(REPO_ROOT)
    config_fp_end = config_fingerprint(_build_config_snapshot(probes))
    validity = evaluate_run_validity(
        revision_start=revision_start,
        revision_end=revision_end,
        config_fingerprint_start=config_fp_start,
        config_fingerprint_end=config_fp_end,
    )

    # 6. 采集与聚合
    task = read_task_row(thread_id) or {
        "thread_id": thread_id,
        "status": final_status.get("status"),
        "attempt": final_status.get("attempt"),
    }
    if "attempt" not in task:
        task["attempt"] = final_status.get("attempt")
    rows, sources = _collect_rows(run_id, thread_id)
    run_json = write_artifacts(
        out_dir, run_id, task, rows, environment,
        {**BASELINE_CASE, "query": args.query, "review_action": args.review_action},
        sources,
        extra={
            "experiment": experiment.to_dict(),
            "integrity": {
                "validity": validity["validity"],
                "reasons": validity["reasons"],
                "code_revision_start": revision_start,
                "code_revision_end": revision_end,
                "config_fingerprint_start": config_fp_start,
                "config_fingerprint_end": config_fp_end,
                "config_snapshot": config_snapshot,
                "preflight_passed": preflight["passed"],
                "preflight_report": str(report_path),
            },
            "runner": {
                "wall_clock_seconds": round(time.time() - started_at, 1),
                "timeout": bool(final_status.get("runner_timeout")),
            },
        },
    )

    print_summary(run_json)
    print(f"validity : {run_json.get('integrity', {}).get('validity')} "
          f"experiment: {run_json.get('experiment')}")
    print(f"artifacts -> {out_dir}")
    if run_json.get("integrity", {}).get("validity") != "VALID":
        print("[FAIL] run 期间代码/配置发生变化，该 run 标记为 INVALID，不得进入统计。", file=sys.stderr)
        return 5
    return 0 if run_json["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
