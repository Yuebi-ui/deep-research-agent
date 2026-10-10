"""Run fingerprint / config fingerprint / experiment identity（Phase 3C P1）。

目的：任何一次 benchmark run 都必须能回答「这是哪一版代码、什么配置、
哪个实验」——且答案可复算、不含密钥。

三个层次：

1. **code revision**：git commit + dirty 标记 + 源码树确定性指纹
   （git 不可用时指纹仍然可用，是 fallback 也是互补：它覆盖未提交修改）；
2. **config fingerprint**：影响 benchmark 行为的非 secret 配置快照
   （角色路由 / thinking 策略 / 上下文预算 / embedding identity / feature
   flags）的稳定 sha256。先经过 :func:`scrub_secrets`，任何 *key*/*token*/
   *secret* 字段被替换为 ``<redacted>``，`sk-...` 形态的值同样被清除；
3. **experiment identity**：experiment_id / variant / kind（baseline /
   experiment / smoke），写进 run.json，杜绝 A/B 混淆。

freeze 检测：run 开始时记录 revision + config fingerprint，结束时重取；
任一变化 → ``INVALID_CODE_CHANGED`` / ``INVALID_CONFIG``，不得进入统计。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# 源码指纹覆盖范围（相对 repo root）——benchmark 行为的全部代码来源
SOURCE_ROOTS = ("deep_research", "backend", "scripts")
SOURCE_SUFFIXES = (".py",)

# 只允许出现在指纹里的环境开关（白名单；绝不整包 dump os.environ）
FEATURE_FLAG_KEYS = (
    "APP_ENV",
    "ALLOW_LIVE_EXTERNAL_APIS",
    "CHECKPOINTER_BACKEND",
    "LLM_PROVIDER",
    "SEARCH_PROVIDER",
    "DR_STREAM_USAGE",
    "DR_BASELINE_LOCAL_CONTEXT_LIMIT",
    # Memory ablation gates, feature policies, durable mode and isolated storage.
    "DR_MEMORY_READ_ENABLED",
    "DR_MEMORY_WRITE_ENABLED",
    "DR_MEMORY_V3_ENABLED",
    "DR_MEMORY_HYBRID_RETRIEVAL",
    "DR_MEMORY_STRUCTURED_RETRIEVAL",
    "DR_MEMORY_TEMPORAL_LINKS",
    "DR_STAGE_MEMORY_ENABLED",
    "DR_EPISODIC_MEMORY_ENABLED",
    "DR_MEMORY_OUTBOX_ENABLED",
    "DR_MEMORY_DATA_DIR",
    "DR_MEMORY_CONTEXT_MAX_CHARS",
    "DR_STAGE_MEMORY_MAX_CHARS",
    # Phase 5B：值 None = 未设置（定版后解析为默认 on，见 research_seed.SEED_LITE_DEFAULT）
    "DR_SEED_LITE_SPECULATIVE",
)

MEMORY_FLAG_DEFAULTS = {
    "DR_MEMORY_READ_ENABLED": "on",
    "DR_MEMORY_WRITE_ENABLED": "on",
    "DR_MEMORY_V3_ENABLED": "on",
    "DR_MEMORY_HYBRID_RETRIEVAL": "on",
    "DR_MEMORY_STRUCTURED_RETRIEVAL": "on",
    "DR_MEMORY_TEMPORAL_LINKS": "on",
    "DR_STAGE_MEMORY_ENABLED": "on",
    "DR_EPISODIC_MEMORY_ENABLED": "on",
    "DR_MEMORY_OUTBOX_ENABLED": "on",
}


def resolved_memory_flags(env: Mapping[str, str]) -> dict[str, bool]:
    """Record effective (not just explicitly set) memory switch semantics."""
    return {
        key: str(env.get(key, default)).strip().lower() not in ("0", "off", "false", "no")
        for key, default in MEMORY_FLAG_DEFAULTS.items()
    }


# thinking 策略开关（调用点级，Phase 3B E1a/E1b 的判定依据）+ 各自默认值
# （默认值必须与 deep_research.llm 的开关语义一致，见 test_thinking_snapshot_matches_llm）
THINKING_ENV_KEYS = {
    "claim_verify": "DR_CLAIM_VERIFY_THINKING",
    "supervisor": "DR_SUPERVISOR_THINKING",
    "writer": "DR_WRITER_THINKING",  # E8：final writer（agent_builder 调用点）
    "draft": "DR_DRAFT_THINKING",  # E9：draft（draft_agent 调用点）
}
THINKING_DEFAULTS = {
    "DR_CLAIM_VERIFY_THINKING": "off",
    "DR_SUPERVISOR_THINKING": "on",
    "DR_WRITER_THINKING": "off",  # E8 KEEP（Phase 3C-2）：final writer 默认 off
    "DR_DRAFT_THINKING": "off",  # E9 KEEP（Phase 3C-3）：draft 默认 off
}


def resolve_thinking(env_var: str, env: Mapping[str, str]) -> bool:
    """把开关环境变量解析成 bool（与 deep_research.llm 同语义）。"""
    value = str(env.get(env_var, THINKING_DEFAULTS.get(env_var, "on"))).strip().lower()
    return value not in ("0", "off", "false", "no")


def thinking_snapshot(env: Mapping[str, str]) -> dict[str, bool]:
    return {name: resolve_thinking(var, env) for name, var in THINKING_ENV_KEYS.items()}

# 角色配置里需要进入指纹的字段（其余字段如 api_key 一律不取）
_ROLE_FIELDS = ("backend", "handle", "temperature", "max_tokens", "extra_body", "chat_template_kwargs")

# 敏感键名：按「词」匹配，避免误伤合法配置——
# reserve_output_tokens / max_tokens 里的 "tokens" 是 token 数量，不是凭据；
# api_key / access_token / TOKEN / secret / password 等必须整词命中才算敏感。
_SECRET_KEY_RE = re.compile(
    r"(^|[^a-z0-9])(api[_-]?key|apikey|secret|token|password|authorization|credentials?)([^a-z0-9]|$)",
    re.I,
)
_SECRET_VALUE_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")

REDACTED = "<redacted>"


# ===== 通用 =====


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scrub_secrets(obj: Any) -> Any:
    """递归清除密钥：key 名命中敏感模式 → 值替换；值形如 ``sk-...`` → 替换。"""
    if isinstance(obj, Mapping):
        out = {}
        for key, value in obj.items():
            if _SECRET_KEY_RE.search(str(key)):
                out[str(key)] = REDACTED
            else:
                out[str(key)] = scrub_secrets(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [scrub_secrets(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VALUE_RE.sub(REDACTED, obj)
    return obj


# ===== 1. code revision =====


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=10, check=False
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:
        return None


def source_fingerprint(repo_root: Path, roots: tuple[str, ...] = SOURCE_ROOTS) -> tuple[str, int]:
    """确定性源码指纹：覆盖范围内每个 .py 的内容哈希按路径排序后聚合。"""
    entries: list[str] = []
    for root in roots:
        base = repo_root / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(repo_root).as_posix()
            entries.append(f"{rel}:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    return _sha256_text("\n".join(entries)), len(entries)


def collect_code_revision(repo_root: Path) -> dict:
    """git revision（若可用）+ 源码指纹（总是可用）。"""
    commit = _git(repo_root, "rev-parse", "HEAD")
    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git(repo_root, "status", "--porcelain")
    dirty_paths = [line[3:] for line in porcelain.splitlines()] if porcelain is not None else []
    fp, count = source_fingerprint(repo_root)
    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(dirty_paths) if porcelain is not None else None,
        "dirty_count": len(dirty_paths) if porcelain is not None else None,
        "dirty_paths": dirty_paths[:10],
        "source_fingerprint": fp,
        "source_file_count": count,
        "git_available": commit is not None,
    }


# ===== 2. config fingerprint =====


def _thinking_snapshot(env: Mapping[str, str]) -> dict:
    """按 llm 默认值 + env 覆盖解析的 thinking 策略。"""
    return thinking_snapshot(env)


def build_config_snapshot(
    stage_cfg: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    embedding: Mapping[str, Any] | None = None,
    context_budget_defaults: Mapping[str, int] | None = None,
) -> dict:
    """构造 benchmark 相关配置快照（已 scrub；可直接写入 artifact）。"""
    env = os.environ if env is None else env

    roles = {}
    for role, cfg in sorted((stage_cfg.get("roles") or {}).items()):
        cfg = cfg or {}
        entry = {k: cfg[k] for k in _ROLE_FIELDS if k in cfg}
        roles[role] = entry

    cognition = {}
    for name, cfg in sorted((stage_cfg.get("cognition") or {}).items()):
        cfg = cfg or {}
        cognition[name] = {
            "base_url": cfg.get("base_url"),
            "handles": sorted((cfg.get("models") or {}).keys()),
        }

    search_cfg = stage_cfg.get("search") or {}
    search = {"backend": search_cfg.get("backend"), "provider": search_cfg.get("provider")}

    budget_cfg = stage_cfg.get("context_budget") or {}
    budget = {
        "config_reserve_output_tokens": budget_cfg.get("reserve_output_tokens"),
        "config_safety_margin_tokens": budget_cfg.get("safety_margin_tokens"),
        "default_reserve_output_tokens": (context_budget_defaults or {}).get("reserve_output_tokens"),
        "default_safety_margin_tokens": (context_budget_defaults or {}).get("safety_margin_tokens"),
    }

    if embedding is None:
        from deep_research.memory.embeddings import EmbeddingClient

        identity = EmbeddingClient().identity
        embedding = {
            "provider": identity.provider,
            "model": identity.model,
            "dimension": identity.dimension,
            "schema_version": identity.schema_version,
        }

    snapshot = {
        "roles": roles,
        "cognition": cognition,
        "search": search,
        "context_budget": budget,
        "thinking": _thinking_snapshot(env),
        "embedding": dict(embedding),
        "feature_flags": {key: env.get(key) for key in FEATURE_FLAG_KEYS},
        "memory_flags_effective": resolved_memory_flags(env),
        "memory_store_dir": env.get("DR_MEMORY_DATA_DIR"),
        # The preset definitions themselves are part of the reproducibility
        # contract even when experiment A/B flags are read from worker env.
        "memory_presets_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[2] / "benchmarks/configs/runtime_ablation.v1.json").read_bytes()
        ).hexdigest(),
    }
    return scrub_secrets(snapshot)


def config_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """稳定指纹：sort_keys + 明确分隔符，跨进程可复算。"""
    canonical = json.dumps(scrub_secrets(dict(snapshot)), sort_keys=True, ensure_ascii=False, default=str)
    return _sha256_text(canonical)


# ===== 3. experiment identity =====


@dataclass(frozen=True)
class ExperimentIdentity:
    experiment_id: str
    variant: str
    kind: str = "baseline"  # baseline | experiment | smoke

    VALID_KINDS = ("baseline", "experiment", "smoke")

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.experiment_id.strip():
            raise ValueError("experiment_id 不能为空")
        if not self.variant or not self.variant.strip():
            raise ValueError("variant 不能为空")
        if self.kind not in self.VALID_KINDS:
            raise ValueError(f"kind 必须是 {self.VALID_KINDS} 之一，得到 {self.kind!r}")

    def to_dict(self) -> dict:
        return {"experiment_id": self.experiment_id, "variant": self.variant, "kind": self.kind}


# ===== freeze / validity =====


def evaluate_run_validity(
    *,
    revision_start: Mapping[str, Any],
    revision_end: Mapping[str, Any],
    config_fingerprint_start: str,
    config_fingerprint_end: str,
) -> dict:
    """比较 run 前后指纹，返回 validity 判定（不修改任何状态）。"""
    reasons: list[str] = []
    if (
        revision_start.get("commit") != revision_end.get("commit")
        or revision_start.get("source_fingerprint") != revision_end.get("source_fingerprint")
    ):
        reasons.append("INVALID_CODE_CHANGED")
    if config_fingerprint_start != config_fingerprint_end:
        reasons.append("INVALID_CONFIG")
    return {
        "validity": "+".join(reasons) if reasons else "VALID",
        "reasons": reasons,
        "checked": True,
    }
