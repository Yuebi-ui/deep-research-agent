"""模型能力探测：context window / tokenizer 路径。

集中唯一实现，避免业务逻辑（context budget、网页总结）到处硬编码 8192。

来源优先级（由调用方组装，本模块只提供事实）：

```text
max_model_len   vLLM /v1/models（root 字段同时给出模型目录，用于找 tokenizer）
tokenizer       模型目录下的 tokenizer.json（tokenizers 库直接加载，无 transformers 依赖）
```

探测失败一律返回 None —— 不猜值。
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from deep_research import logging as dr_logging
from deep_research.settings import get_engine_settings

logger = dr_logging.get_logger(__name__)

# 显式覆盖（部署/测量用）；优先级高于探测
TOKENIZER_PATH_ENV = "DR_TOKENIZER_PATH"


@dataclass(frozen=True)
class LocalModelCapability:
    """本地 OpenAI 兼容服务的一次探测结果。"""

    model_id: str | None
    max_model_len: int | None
    model_root: str | None
    base_url: str


_probe_cache: dict[str, LocalModelCapability | None] = {}


def local_backend_info() -> tuple[str, str]:
    """读取本地 backend 的 base_url / api_key（仅用于本机探测，不落盘）。"""
    try:
        from deep_research.utils import load_config

        cfg = load_config(stage_name=get_engine_settings().stage)
        local_cfg = (cfg.get("cognition") or {}).get("openai_local") or {}
        return str(local_cfg.get("base_url") or ""), str(local_cfg.get("api_key") or "")
    except Exception:
        return "", ""


def probe_local_model(base_url: str, api_key: str = "", timeout: float = 2.0) -> LocalModelCapability | None:
    """探测本地服务的模型能力；失败返回 None（带进程内缓存）。

    测试环境（APP_ENV=test）不发起网络探测，直接返回 None。
    """
    if base_url in _probe_cache:
        return _probe_cache[base_url]

    result: LocalModelCapability | None = None
    if os.environ.get("APP_ENV") == "test" or not base_url:
        _probe_cache[base_url] = None
        return None

    try:
        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310（本机服务）
            payload = json.loads(resp.read().decode("utf-8"))
        for item in payload.get("data") or []:
            max_len = None
            for key in ("max_model_len", "max_context_length", "context_length"):
                value = item.get(key)
                if isinstance(value, int) and value > 0:
                    max_len = value
                    break
            result = LocalModelCapability(
                model_id=item.get("id"),
                max_model_len=max_len,
                model_root=item.get("root"),
                base_url=base_url,
            )
            break
    except Exception as exc:
        logger.debug("模型能力探测失败（不影响业务）: %s", exc)

    _probe_cache[base_url] = result
    return result


def probe_local_context_limit(base_url: str, api_key: str = "", timeout: float = 2.0) -> int | None:
    """仅取 max_model_len 的便捷包装（Phase 1 观测接口保持稳定）。"""
    capability = probe_local_model(base_url, api_key, timeout)
    return capability.max_model_len if capability else None


def resolve_tokenizer_path(model_root: str | None = None) -> str | None:
    """定位本地模型 tokenizer.json：环境变量优先，其次探测到的模型目录。"""
    env_path = os.environ.get(TOKENIZER_PATH_ENV)
    if env_path:
        return env_path if Path(env_path).exists() else None

    if model_root:
        candidate = Path(model_root) / "tokenizer.json"
        if candidate.exists():
            return str(candidate)

    return None


def reset_capability_cache() -> None:
    """清空探测缓存（测试隔离用）。"""
    _probe_cache.clear()


__all__ = [
    "TOKENIZER_PATH_ENV",
    "LocalModelCapability",
    "local_backend_info",
    "probe_local_context_limit",
    "probe_local_model",
    "reset_capability_cache",
    "resolve_tokenizer_path",
]
