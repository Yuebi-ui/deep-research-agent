"""Benchmark 启动前 self-check（Phase 3C P1）。

原则：**FAIL FAST**。不运行一个「可能配置不对」的 6 分钟 benchmark。

检查项（任何一项与实验预期不符 → passed=False，runner 拒绝启动任务）：

1. API health（含 memory_schema 组件）
2. Redis 可达
3. vLLM 可达且能报出 model / max_model_len
4. 本地模型 id == 预期
5. max context == 预期（当前 comparison baseline = 8192）
6. thinking 策略 == 预期（优先读 **worker 进程实际环境**，
   而不是 runner 自己的环境——历史事故：服务没重启导致旧配置运行）
7. embedding identity == 预期（provider/model/dimension/schema）
8. persisted memory schema 全部 compatible（旧 space 会在运行期才暴露）
9. worker 进程存活
10. 服务新鲜度：worker/API 进程启动时间必须晚于最新源码 mtime
    （历史事故：改完代码忘记重启服务 → 空跑/旧配置运行）

所有 probe 可注入（测试用 fake），默认 implementation 见 :class:`SystemProbes`。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from deep_research import logging as dr_logging

logger = dr_logging.get_logger(__name__)


@dataclass(frozen=True)
class Expectations:
    """本次 benchmark 声明的实验预期。None 字段 = 不检查。"""

    local_model: str | None = None
    context_limit: int | None = None
    thinking: Mapping[str, bool] = field(default_factory=dict)  # {"claim_verify": False, ...}
    embedding: Mapping[str, Any] = field(default_factory=dict)  # provider/model/dimension/schema_version
    require_worker: bool = True
    allow_stale_services: bool = False


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    expected: Any = None
    observed: Any = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "expected": self.expected,
            "observed": self.observed,
        }


# ===== 默认 probes（真实系统）=====


def _get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _process_info(pid: int) -> dict | None:
    """从 /proc 读进程启动时间与（尽力而为的）环境变量。"""
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return None
    info: dict[str, Any] = {"pid": pid}
    try:
        stat = (proc_dir / "stat").read_text()
        # comm 可能含空格/括号：取最后一个 ')' 之后再按空白切
        fields_after_comm = stat[stat.rfind(")") + 2 :].split()
        starttime_ticks = int(fields_after_comm[19])  # 第 22 个字段（1-based）
        hz = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        boot_epoch = time.time() - uptime
        info["started_at"] = boot_epoch + starttime_ticks / hz
    except Exception:
        info["started_at"] = None
    try:
        raw = (proc_dir / "environ").read_bytes().decode("utf-8", errors="replace")
        info["env"] = dict(
            item.split("=", 1) for item in raw.split("\0") if "=" in item
        )
    except Exception:
        info["env"] = None
    try:
        info["cmdline"] = (proc_dir / "cmdline").read_bytes().decode("utf-8", errors="replace").replace("\0", " ").strip()
    except Exception:
        info["cmdline"] = None
    return info


class SystemProbes:
    """真实系统探测。任何单项失败都返回 None/异常安全值，由 check 层判定。"""

    def __init__(self, api_base: str, repo_root: Path, run_dir: Path) -> None:
        self.api_base = api_base
        self.repo_root = repo_root
        self.run_dir = run_dir

    # -- 服务 --

    def api_health(self) -> dict | None:
        try:
            return _get_json(f"{self.api_base}/api/health")
        except Exception as exc:  # noqa: BLE001
            return {"status": f"unreachable: {str(exc)[:120]}", "components": {}}

    def redis_ping(self) -> bool | None:
        import socket

        host = os.environ.get("REDIS_HOST", "127.0.0.1")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        try:
            with socket.create_connection((host, port), timeout=3) as sock:
                sock.sendall(b"PING\r\n")
                return sock.recv(64).startswith(b"+PONG")
        except Exception:
            return False

    def _local_base_url(self) -> str | None:
        try:
            from deep_research.settings import get_engine_settings
            from deep_research.utils import load_config

            cfg = load_config(stage_name=get_engine_settings().stage)
            local = (cfg.get("cognition") or {}).get("openai_local") or {}
            return str(local.get("base_url") or "") or None
        except Exception:
            return None

    def vllm_models(self) -> dict | None:
        base_url = self._local_base_url()
        if not base_url:
            return None
        try:
            payload = _get_json(base_url.rstrip("/") + "/models", timeout=5)
        except Exception:
            return None
        for item in payload.get("data") or []:
            model = item.get("id")
            max_len = next(
                (item[k] for k in ("max_model_len", "max_context_length") if isinstance(item.get(k), int)),
                None,
            )
            return {"model": model, "max_model_len": max_len, "base_url": base_url}
        return {"model": None, "max_model_len": None, "base_url": base_url}

    # -- 进程 --

    def _service_process(self, name: str) -> dict | None:
        pid_file = self.run_dir / f"{name}.pid"
        if not pid_file.exists():
            return None
        try:
            pid = int(pid_file.read_text().splitlines()[0].strip())
        except Exception:
            return None
        info = _process_info(pid)
        if info is None:
            return {"pid": pid, "alive": False, "started_at": None, "env": None, "cmdline": None}
        info["alive"] = True
        return info

    def worker_process(self) -> dict | None:
        return self._service_process("worker")

    def api_process(self) -> dict | None:
        return self._service_process("api")

    # -- 配置 / 存储 --

    def embedding_identity(self) -> dict:
        from deep_research.memory.embeddings import EmbeddingClient

        identity = EmbeddingClient().identity
        return {
            "provider": identity.provider,
            "model": identity.model,
            "dimension": identity.dimension,
            "schema_version": identity.schema_version,
        }

    def memory_schema(self) -> list[dict] | None:
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            from deep_research.memory.migration import MANAGED_COLLECTIONS
            from deep_research.memory.schema_guard import inspect_collection
            from deep_research.settings import get_engine_settings

            persist_dir = get_engine_settings().resolved_data_dir / "chroma"
            if not persist_dir.exists():
                return []
            client = chromadb.PersistentClient(
                path=str(persist_dir), settings=ChromaSettings(anonymized_telemetry=False)
            )
            expected = self.embedding_identity()
            from deep_research.memory.embeddings import EmbeddingIdentity

            identity = EmbeddingIdentity(**expected)
            return [inspect_collection(client, name, identity) for name, _ in MANAGED_COLLECTIONS]
        except Exception:
            return None

    def newest_source_mtime(self) -> float | None:
        from deep_research.benchmark.fingerprint import SOURCE_ROOTS

        newest: float | None = None
        for root in SOURCE_ROOTS:
            base = self.repo_root / root
            if not base.exists():
                continue
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                m = path.stat().st_mtime
                newest = m if newest is None or m > newest else newest
        return newest


# ===== checks =====


def _process_summary(proc: dict | None) -> dict | None:
    """进程信息摘要：绝不携带 env 值（worker 环境含 API key），只留键名。"""
    if proc is None:
        return None
    summary = {k: v for k, v in proc.items() if k != "env"}
    env = proc.get("env")
    summary["env_keys"] = sorted(env.keys()) if isinstance(env, Mapping) else None
    return summary


def _run_checks(expectations: Expectations, probes: Any) -> tuple[list[Check], dict]:
    checks: list[Check] = []
    observations: dict[str, Any] = {}

    # 1. API health
    health = probes.api_health()
    observations["api_health"] = health
    status = (health or {}).get("status")
    checks.append(
        Check("api_health", status == "ok", f"/api/health status={status}", "ok", status)
    )

    # 2. Redis
    redis_ok = probes.redis_ping()
    checks.append(Check("redis", redis_ok is True, f"redis ping -> {redis_ok}", True, redis_ok))

    # 3 & 4 & 5. vLLM / local model / context limit
    models = probes.vllm_models()
    observations["vllm"] = models
    checks.append(Check("vllm", models is not None, f"vllm /v1/models -> {models}", "reachable", models))
    observed_model = (models or {}).get("model")
    if expectations.local_model is not None:
        checks.append(
            Check(
                "local_model",
                observed_model == expectations.local_model,
                f"local model = {observed_model!r}",
                expectations.local_model,
                observed_model,
            )
        )
    observed_ctx = (models or {}).get("max_model_len")
    if expectations.context_limit is not None:
        checks.append(
            Check(
                "context_limit",
                observed_ctx == expectations.context_limit,
                f"max_model_len = {observed_ctx!r}",
                expectations.context_limit,
                observed_ctx,
            )
        )

    # 6. thinking 策略：优先 worker 进程环境（权威），退回 runner 环境
    worker = probes.worker_process()
    observations["worker_process"] = _process_summary(worker)
    env_source = "runner_env"
    env_map: Mapping[str, str] = os.environ
    if worker and worker.get("env"):
        env_source = "worker_env"
        env_map = worker["env"]
    # 7.1：feature flags 以 **worker 进程环境**为准（runner shell 常常没加载
    # .env.server，只有 worker 的环境才是 benchmark 实际运行的配置）。
    from deep_research.benchmark.fingerprint import FEATURE_FLAG_KEYS

    observations["worker_feature_flags"] = {key: env_map.get(key) for key in FEATURE_FLAG_KEYS}

    if expectations.thinking:
        observed_thinking = {
            name: _resolve_thinking(env_var, env_map)
            for name, env_var in _thinking_env_map().items()
            if name in expectations.thinking
        }
        ok = all(observed_thinking[k] == bool(v) for k, v in expectations.thinking.items())
        checks.append(
            Check(
                "thinking_policy",
                ok,
                f"thinking({env_source}) = {observed_thinking}",
                dict(expectations.thinking),
                observed_thinking,
            )
        )
        observations["thinking_env_source"] = env_source

    # 7. embedding identity
    if expectations.embedding:
        identity = probes.embedding_identity()
        observations["embedding_identity"] = identity
        ok = all(identity.get(k) == v for k, v in expectations.embedding.items())
        checks.append(
            Check("embedding_identity", ok, f"embedding = {identity}", dict(expectations.embedding), identity)
        )

    # 8. memory schema
    schema = probes.memory_schema()
    observations["memory_schema"] = schema
    if schema is not None:
        bad = [r for r in schema if not r.get("compatible")]
        checks.append(
            Check(
                "memory_schema",
                not bad,
                "all collections compatible" if not bad else f"incompatible: {[r['collection'] for r in bad]}",
                "compatible",
                [r["status"] for r in schema],
            )
        )

    # 9. worker 存活
    if expectations.require_worker:
        alive = bool(worker and worker.get("alive"))
        checks.append(
            Check(
                "worker_process",
                alive,
                "worker alive" if alive else f"worker not running (probe={_process_summary(worker)})",
                "alive",
                _process_summary(worker),
            )
        )

    # 10. 服务新鲜度：进程启动时间必须晚于最新源码 mtime
    if not expectations.allow_stale_services:
        newest = probes.newest_source_mtime()
        processes = {"worker": worker, "api": probes.api_process()}
        observations["processes"] = processes
        known = {name: p for name, p in processes.items() if p and p.get("started_at")}
        if newest is not None and known:
            stale = [name for name, p in known.items() if p["started_at"] < newest]
            checks.append(
                Check(
                    "service_freshness",
                    not stale,
                    "服务启动时间晚于最新源码" if not stale
                    else f"{stale} 进程启动早于最新源码修改——先重启服务再 benchmark",
                    "fresh",
                    {"stale": stale},
                )
            )

    return checks, observations


def _thinking_env_map() -> dict[str, str]:
    from deep_research.benchmark.fingerprint import THINKING_ENV_KEYS

    return dict(THINKING_ENV_KEYS)


def _resolve_thinking(env_var: str, env_map: Mapping[str, str]) -> bool:
    """与 deep_research.llm 的默认值保持一致（共用 fingerprint.resolve_thinking）。"""
    from deep_research.benchmark.fingerprint import resolve_thinking

    return resolve_thinking(env_var, env_map)


def run_preflight(
    expectations: Expectations,
    probes: Any,
    *,
    raise_on_failure: bool = False,
) -> dict:
    """执行全部检查，返回 {"passed", "checks", "observations"}。"""
    from deep_research.benchmark.fingerprint import scrub_secrets

    checks, observations = _run_checks(expectations, probes)
    failed = [c for c in checks if not c.ok]
    report = scrub_secrets({
        "passed": not failed,
        "checks": [c.to_dict() for c in checks],
        "observations": _jsonable(observations),
        "failed": [c.name for c in failed],
    })
    if failed and raise_on_failure:
        details = "; ".join(f"{c.name}: {c.detail}" for c in failed)
        raise RuntimeError(f"preflight failed: {details}")
    return report


def _jsonable(value: Any) -> Any:
    """probes 可能带回 /proc 环境等不可序列化结构：只保留 JSON 安全的部分。"""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items() if k != "env"}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
