#***********************************************
#      Filename: llm.py
#   Description: 大模型客户端 
#***********************************************


from __future__ import annotations

import os
from typing import Any, Dict, Optional
from langchain.chat_models import init_chat_model

from deep_research.utils import load_config
from deep_research import logging as dr_logging


# 初始化logger
logger = dr_logging.get_logger(__name__)

# 缓存CONFIG，避免重复导入(config_path, stage, loader_id) 
_CONFIG_CACHE: Dict[tuple[str, str, int], Dict[str, Any]] = {}

# 默认的stage
DEFAULT_STAGE = "prod"


class LLMConfigError(ValueError):
    """当LLM配置错误或者不合法时抛出该异常"""


def _resolve_stage(stage: str | None) -> str:
    return stage or os.environ.get("STAGE") or DEFAULT_STAGE


def _load_stage_config(stage_name: str | None, config_path: str | None) -> Dict[str, Any]:
    """加载config.yml"""

    # Key作为config loader的唯一标识
    cache_key = (os.environ.get("CONFIG_PATH", "config.yml"), stage_name, id(load_config))

    if cache_key in _CONFIG_CACHE:
        return _CONFIG_CACHE[cache_key]

    cfg = load_config(stage_name=stage_name, config_path=config_path)
    if cfg is None:
        raise LLMConfigError(f"No config found for stage '{stage_name}'")

    _CONFIG_CACHE[cache_key] = cfg
    return cfg


def _build_openai_kwargs(
    handle: str,
    api_cfg: Dict[str, Any],
    max_tokens: int | None,
    timeout_seconds: Optional[int],
    thinking: bool | None = None,
) -> Dict[str, Any]:
    """初始化llm client参数，例如api_key, base_url

    ``thinking``：仅按 **调用点** 显式指定时的 thinking 策略（Phase 3 E1a）。
    ``None`` 表示不干预（保持 provider 默认，与历史行为一致）。
    """

    model = handle or api_cfg.get("default_model")
    if not model:
        raise LLMConfigError("OpenAI config requires a model name!")

    kwargs: Dict[str, Any] = {
        "model": model,
        "model_provider": "openai",
    }

    # api_key, base_url
    for key in ("api_key", "base_url", "organization"):
        if api_cfg.get(key):
            kwargs[key] = api_cfg[key]

    # 温度系数
    if api_cfg.get("temperature") is not None:
        kwargs["temperature"] = api_cfg["temperature"]

    # 最大token数
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    # 请求超时
    if timeout_seconds is not None:
        kwargs["timeout"] = timeout_seconds

    # backend 级 extra_body 透传（例如本地 vLLM 的 chat_template_kwargs）
    extra_body: Dict[str, Any] = dict(api_cfg.get("extra_body") or {})

    # qwen3-32b 非流式调用必须显式关闭思考模式
    if model.startswith("qwen3-32b"):
        extra_body.setdefault("enable_thinking", False)

    # E1a：按调用点的 thinking 策略（DashScope 兼容层接受 enable_thinking）
    if thinking is False:
        extra_body.setdefault("enable_thinking", False)

    if extra_body:
        kwargs["extra_body"] = extra_body

    # 观测开关（默认关闭）：要求 OpenAI 兼容端点在流式响应末尾附带 usage。
    #
    # 背景：在本项目架构下 worker 用 astream_events 驱动图，LangChain 会把
    # 子模型调用统一转成 streaming；而对自定义 base_url，langchain-openai 的
    # stream_usage 默认为 False → provider 不返回 usage → token 指标不可得。
    # 打开后仅追加 `stream_options={"include_usage": true}`，
    # **不改变生成内容/采样参数**，只让 usage 可观测。
    # 两个端点（vLLM / DashScope）的兼容性已在真实环境验证。
    if os.environ.get("DR_STREAM_USAGE", "").strip().lower() in ("1", "true", "yes"):
        kwargs.setdefault("stream_usage", True)

    return kwargs


def _resolve_config_max_tokens(api_cfg: Dict[str, Any], handle: str) -> int | None:
    """解析最大token数"""
    models_cfg = api_cfg.get("models") or {}
    model_cfg = models_cfg.get(handle) or {}
    return model_cfg.get("max_tokens")


def _resolve_timeout_seconds(api_cfg: Dict[str, Any], role_cfg: Dict[str, Any]) -> Optional[int]:
    """解析timeout/request_timeout/timeout_seconds参数"""
    for cfg in (role_cfg, api_cfg):
        for key in ("timeout", "request_timeout", "timeout_seconds"):
            if cfg.get(key) is not None:
                return cfg.get(key)
    return None


# 使用 OpenAI 客户端构建的 backend：
#   openai        —— 云端 OpenAI 兼容服务（如 DashScope compatible-mode）
#   openai_local  —— 本地 OpenAI 兼容服务（vLLM，见 scripts/model-service/）
# 两者只是 base_url / api_key / model 不同，客户端构建逻辑完全一致；
# 切换 provider 只需改 roles 里的 backend，Agent 代码无感知。
_OPENAI_COMPATIBLE_BACKENDS = frozenset({"openai", "openai_local"})


def _build_kwargs(
    backend: str,
    handle: str,
    api_cfg: Dict[str, Any],
    role_cfg: Dict[str, Any],
    max_tokens: int | None,
    timeout_seconds: int | None,
    thinking: bool | None = None,
) -> Dict[str, Any]:

    if backend in _OPENAI_COMPATIBLE_BACKENDS:
        return _build_openai_kwargs(handle, api_cfg, max_tokens, timeout_seconds, thinking=thinking)
    else:
        raise LLMConfigError(f"Unsupported backend '{backend}'")


# 智能路由：query 字符数小于此值使用小模型
_ROUTING_COMPLEXITY_THRESHOLD = 100
_SIMPLE_MODEL_ROLE = "evaluator"
_COMPLEX_MODEL_ROLE = "writer"


# 任务类型 → 模型角色映射（规划/写作→235b，摘要/抽取→32b）
_TASK_ROLE_MAP = {
    "planning": "supervisor",
    "drafting": "writer",
    "summarizing": "researcher_compressor",
    "extracting": "evaluator",
    "verifying": "evaluator",
    "researching": "researcher_main",
    "critiquing": "red_team",
}


def get_chat_model_for_task(
    task_type: str,
    *,
    stage: str | None = None,
    max_tokens: int | None = None,
    thinking: bool | None = None,
):
    """根据任务类型智能路由模型。规划/写作/研究 → 235b，摘要/抽取/验证/批评 → 32b。

    ``thinking``：仅按调用点显式指定（E1a）；None 保持 provider 默认。
    """
    role = _TASK_ROLE_MAP.get(task_type)
    if role is None:
        logger.warning("Unknown task_type '%s', falling back to writer", task_type)
        role = "writer"
    logger.info("Task routing: '%s' → role '%s'", task_type, role)
    return get_chat_model(role, stage=stage, max_tokens=max_tokens, thinking=thinking)


def get_chat_model_auto(role: str, query_text: str = "", *, stage: str | None = None, max_tokens: int | None = None):
    """根据 query 复杂度自动路由模型。短 query → qwen3-32b，长 query → qwen3-235b。"""
    model_role = _SIMPLE_MODEL_ROLE if len(query_text) < _ROUTING_COMPLEXITY_THRESHOLD else _COMPLEX_MODEL_ROLE
    logger.info("Smart routing: role '%s' (query_len=%d) → model_role '%s'", role, len(query_text), model_role)
    return get_chat_model(model_role, stage=stage, max_tokens=max_tokens)


def get_chat_model(
    role: str,
    *,
    stage: str | None = None,
    max_tokens: int | None = None,
    thinking: bool | None = None,
):
    """根据config和role返回LLM client.

    Args:
        role: 角色名，例如supervisor, writer
        stage: stage name
        max_tokens: 最大tokens
        thinking: 按调用点的 thinking 策略（E1a）。None=不干预（provider 默认）；
            False=显式关闭（仅对 OpenAI 兼容 backend 生效：extra_body.enable_thinking=false）
    """

    from deep_research.settings import get_engine_settings

    settings = get_engine_settings()

    # 离线短路必须发生在任何 config 读取之前：
    # 测试/CI 环境不提供 config.yml，且不得构造真实 provider。
    if settings.use_fake_llm:
        from deep_research.testing.fake_llm import make_fake_chat_model

        model = make_fake_chat_model(role)
        return _attach_cost_callback(model)

    # 获取config路径
    config_path = str(settings.resolved_config_path)
    resolved_stage = _resolve_stage(stage)

    # 加载config.yam
    cfg = _load_stage_config(resolved_stage, config_path)

    # 获取role配置 
    roles_cfg = cfg.get("roles", {})
    if role not in roles_cfg:
        # 清除cache重新加载一次
        _CONFIG_CACHE.clear()
        cfg = _load_stage_config(resolved_stage, config_path)
        roles_cfg = cfg.get("roles", {})

    # 如果role配置错误
    if role not in roles_cfg:
        available = ", ".join(sorted(roles_cfg.keys())) or "<none>"
        raise LLMConfigError(
            f"Role '{role}' not found for stage '{resolved_stage}' using config '{config_path}'. Available: {available}"
        )

    # 解析backend和handle
    role_cfg = roles_cfg[role]
    backend = role_cfg.get("backend")
    handle = role_cfg.get("handle")
    if not backend or not handle:
        raise LLMConfigError(f"Role '{role}' is missing backend or handle")

    # 解析llm api config
    api_cfg = cfg.get("cognition", {}).get(backend)
    if api_cfg is None:
        raise LLMConfigError(f"No cognition config for backend '{backend}'")

    # 获取超时时间
    resolved_timeout = _resolve_timeout_seconds(api_cfg, role_cfg)
    logger.info(
        "Selected cognition backend '%s' for role '%s' with handle '%s' (timeout=%s, thinking=%s)",
        backend,
        role,
        handle,
        resolved_timeout,
        "default" if thinking is None else ("on" if thinking else "off"),
    )

    # 获取输出最大token数
    resolved_max_tokens = max_tokens
    if resolved_max_tokens is None:
        resolved_max_tokens = _resolve_config_max_tokens(api_cfg, handle)

    # 新建llm client
    kwargs = _build_kwargs(
        backend=backend,
        handle=handle,
        api_cfg=api_cfg,
        role_cfg=role_cfg,
        max_tokens=resolved_max_tokens,
        timeout_seconds=resolved_timeout,
        thinking=thinking,
    )
    model = init_chat_model(**kwargs)
    return _attach_cost_callback(model, role=role, backend=backend, handle=handle, thinking=thinking)


def _role_metadata(role: str, backend: str, handle: str, thinking: bool | None = None) -> Dict[str, str]:
    """观测用 role 元数据（baseline 指标归因需要）。

    只影响 LangChain run metadata，不进入请求体、不改变模型行为。
    ``dr_thinking`` 仅在调用点显式指定时记录（on/off），供指标审计。
    """
    meta = {"dr_role": role, "dr_backend": backend, "dr_handle": handle}
    if thinking is not None:
        meta["dr_thinking"] = "on" if thinking else "off"
    return meta


def _attach_cost_callback(
    model,
    role: str | None = None,
    backend: str | None = None,
    handle: str | None = None,
    thinking: bool | None = None,
):
    """给模型挂上当前的成本回调与 role 元数据（若有）。

    真实 provider 与 Fake provider 共用同一套挂载逻辑。
    role 元数据仅附加给真实 provider —— Fake 模型保持原始对象，
    避免 with_config 包装破坏 ``isinstance(model, FakeChatModel)`` 约定。
    """
    config: Dict[str, Any] = {}
    try:
        from deep_research.callbacks.cost_tracker import get_cost_callback
        cb = get_cost_callback()
        if cb is not None:
            config["callbacks"] = [cb]
    except ImportError:
        pass
    if role and backend and handle and not _is_fake_model(model):
        config["metadata"] = _role_metadata(role, backend, handle, thinking=thinking)
    if config:
        model = model.with_config(config)
    return model


# ===== E1a：claim extractor / judge 的 thinking 策略开关 =====
#
# 只控制 **这两个调用点**（它们是独立的 call-site，不是整个 evaluator role）。
# Phase 3B E1a A/B 结论（2026-10-06）：OFF 后 extractor -80% 延迟、judge -49%、
# claim_verification 节点 -34%，质量与证据覆盖不降（含受控微实验）→ KEEP，
# 因此默认值翻转为 "off"。需要恢复旧行为时显式设 DR_CLAIM_VERIFY_THINKING=on。
_CLAIM_VERIFY_THINKING_ENV = "DR_CLAIM_VERIFY_THINKING"


def claim_verify_thinking() -> bool:
    """claim extractor/judge 是否启用 thinking（E1a KEEP 后默认 off）。"""
    value = os.environ.get(_CLAIM_VERIFY_THINKING_ENV, "off").strip().lower()
    return value not in ("0", "off", "false", "no")


# ===== E1b：supervisor 的 thinking 策略开关 =====
_SUPERVISOR_THINKING_ENV = "DR_SUPERVISOR_THINKING"


def supervisor_thinking() -> bool:
    """supervisor 是否启用 thinking（E1b 实验开关；默认 on = 历史行为）。"""
    value = os.environ.get(_SUPERVISOR_THINKING_ENV, "on").strip().lower()
    return value not in ("0", "off", "false", "no")


# ===== E8：final writer 的 thinking 策略开关 =====
#
# 作用域**仅限 final writer 调用点**（`agent_builder.writer_model`）。
# "writer" role 还有两个独立消费者，**刻意不接本开关**（单变量要求：
# 它们在任何 variant 下都保持相同配置）：
#
#   1. `tools/tool.py` 的 `refine_draft_report`（supervisor 在 research
#      阶段内调用它迭代草稿）；
#   2. `get_chat_model_auto("draft", ...)` 在 query 较长时路由到 writer role。
#
# 影响面声明：`agent_builder.writer_model` 同时服务 final_report_generation
# 与 HITL 的 revise 分支（benchmark 固定走 approve，不触发 revise）。
#
# Phase 3C-2 E8 A/B 结论（2026-10-06，6 次 VALID E2E + 2 状态 micro replay）：
#   E2E 延迟 377.8s→311.9s（median -17.4%，3/3 对一致）、writer 单调用
#   112.1s→74.1s（-33.9%）、reasoning tokens -30%、云端成本 -17.4%；
#   质量：受控 micro（同输入）两臂 claim 覆盖完全相同、独立 judge 5 tie + 1 弱 ON。
#   → KEEP，默认翻转为 "off"。需要恢复旧行为时显式设 DR_WRITER_THINKING=on。
_WRITER_THINKING_ENV = "DR_WRITER_THINKING"


def writer_thinking() -> bool:
    """final writer 是否启用 thinking（E8 KEEP 后默认 off）。"""
    value = os.environ.get(_WRITER_THINKING_ENV, "off").strip().lower()
    return value not in ("0", "off", "false", "no")


# ===== E9：draft 的 thinking 策略开关 =====
#
# 作用域**仅限 logical role = draft 的唯一调用点**：
#   `draft_agent.draft_model`（write_draft_report 节点，import-time 构造）。
#
# 刻意不覆盖（E9 单变量要求，A/B 两组保持相同）：
#   1. `agent_builder.write_research_brief` 的 `get_chat_model_auto("draft")`
#      —— 它按 query 复杂度路由到 evaluator/writer role，是**另一个 logical
#      role 的调用点**（benchmark query 实际路由到 evaluator）；
#   2. `tools/tool.py` 的 `refine_draft_report`（research 阶段内的草稿迭代，
#      用的是 writer role）。
#
# Phase 3C-3 E9 A/B 结论（2026-10-06，6 次 VALID E2E + 固定 brief micro）：
#   draft 节点延迟 90.8s→68.0s（median -25.1%，3/3 对一致）、draft reasoning →0、
#   总 reasoning -21.7%、云端成本 -12.1%；E2E -5.6%（2/3 对）；
#   **研究深度护栏完全等价**（supervisor 3/3、researcher iterations 10、tool 8、
#   search 14、evidence 10、claims 10——无 contraction）；最终报告质量持平
#   （claim 覆盖 100% vs 100%/89%、unsupported 无上升、独立 judge 2/3 偏 OFF）。
#   → KEEP，默认翻转为 "off"。显式 DR_DRAFT_THINKING=on 可恢复旧行为。
_DRAFT_THINKING_ENV = "DR_DRAFT_THINKING"


def draft_thinking() -> bool:
    """draft（write_draft_report）是否启用 thinking（E9 KEEP 后默认 off）。"""
    value = os.environ.get(_DRAFT_THINKING_ENV, "off").strip().lower()
    return value not in ("0", "off", "false", "no")


def _is_fake_model(model: Any) -> bool:
    try:
        from deep_research.testing.fake_llm import FakeChatModel

        return isinstance(model, FakeChatModel)
    except ImportError:
        return False


def with_role_metadata(runnable, role: str, *, stage: str | None = None):
    """为 bind_tools 之后的可运行对象补挂 role 元数据。

    背景：``bind_tools()`` 会返回新的 runnable，构造期的 metadata 不会自动
    带过去（supervisor / research_agent 的工具绑定模型走这条路径）。
    仅用于观测归因；任何解析失败都原样返回，不改变行为。
    """
    if _is_fake_model(runnable):
        return runnable
    try:
        from deep_research.settings import get_engine_settings

        cfg = _load_stage_config(_resolve_stage(stage), str(get_engine_settings().resolved_config_path))
        role_cfg = (cfg.get("roles") or {}).get(role) or {}
        backend = role_cfg.get("backend")
        handle = role_cfg.get("handle")
        if backend and handle:
            return runnable.with_config({"metadata": _role_metadata(role, backend, handle)})
    except Exception as exc:  # noqa: BLE001 —— 观测归因失败不影响主流程
        logger.debug("role metadata 附加失败（忽略）: %s", exc)
    return runnable
