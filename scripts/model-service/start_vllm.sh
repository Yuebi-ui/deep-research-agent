#!/usr/bin/env bash
# 启动本地 vLLM Model Service（OpenAI-compatible，默认 127.0.0.1:8001）。
#
#     bash scripts/model-service/start_vllm.sh
#
# ⚠️ 需要 GPU（RTX 4090D）。无卡模式下会在前置检查处直接报错退出，
#    这是预期行为，不是脚本 bug。
#
# 参数可通过环境变量覆盖（见 _common.sh），例如：
#     VLLM_MAX_MODEL_LEN=16384 bash scripts/model-service/start_vllm.sh
#
# GPU 阶段待验证项（不算失败，验证后按需调整）：
#   * --tool-call-parser hermes —— Qwen3 工具调用解析器
#   * chat_template_kwargs.enable_thinking=false（在应用侧 config 中）
#   * max_model_len / gpu_memory_utilization 的最终取值

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/model-service/_common.sh
. ./_common.sh

# 工具调用：research_agent / supervisor 使用 bind_tools()，需要 vLLM 开启
# auto tool choice，否则请求带 tools 参数会被拒绝。
VLLM_TOOL_CALL_PARSER="${VLLM_TOOL_CALL_PARSER:-hermes}"
VLLM_ENABLE_TOOL_CHOICE="${VLLM_ENABLE_TOOL_CHOICE:-1}"

section "0. 前置检查"

[ -x "${VLLM_VENV}/bin/vllm" ] || die "vLLM 环境不存在：${VLLM_VENV}
  请先运行：bash scripts/model-service/setup_env.sh"

[ -f "${VLLM_MODEL_PATH}/config.json" ] || die "模型目录不完整：${VLLM_MODEL_PATH}
  请确认模型已下载到数据盘（见 scripts/model-service/README.md）"
ls "${VLLM_MODEL_PATH}"/*.safetensors >/dev/null 2>&1 \
  || die "模型权重（.safetensors）缺失：${VLLM_MODEL_PATH}"

command -v nvidia-smi >/dev/null 2>&1 || die "找不到 nvidia-smi —— 这不是 GPU 机器？"
if ! nvidia-smi -L 2>/dev/null | grep -q "GPU"; then
  die "未检测到 GPU（当前是无卡模式）。
  这是无卡准备阶段的预期状态：脚本本身已就绪，
  开启 RTX 4090D 后直接重跑本脚本即可。"
fi
info "GPU：$(nvidia-smi -L | head -1)"

if is_running; then
  warn "vLLM 已在运行（pid $(running_pid)），跳过启动"
  info "健康检查：bash scripts/model-service/healthcheck_vllm.sh"
  exit 0
fi

if port_in_use "$VLLM_PORT"; then
  die "端口 ${VLLM_PORT} 已被占用（不是本脚本启动的 vLLM？）
  查看：ss -ltnp | grep ${VLLM_PORT}"
fi

section "1. 启动 vLLM"
info "模型：${VLLM_MODEL_PATH}"
info "端口：${VLLM_HOST}:${VLLM_PORT}（served-model-name=${VLLM_SERVED_NAME}）"
info "参数：max_model_len=${VLLM_MAX_MODEL_LEN} gpu_memory_utilization=${VLLM_GPU_MEMORY_UTILIZATION}"

ARGS=(
  serve "${VLLM_MODEL_PATH}"
  --host "${VLLM_HOST}"
  --port "${VLLM_PORT}"
  --served-model-name "${VLLM_SERVED_NAME}"
  --max-model-len "${VLLM_MAX_MODEL_LEN}"
  --gpu-memory-utilization "${VLLM_GPU_MEMORY_UTILIZATION}"
)
if [ "${VLLM_ENABLE_TOOL_CHOICE}" = "1" ]; then
  ARGS+=(--enable-auto-tool-choice --tool-call-parser "${VLLM_TOOL_CALL_PARSER}")
fi

nohup "${VLLM_VENV}/bin/vllm" "${ARGS[@]}" >> "${VLLM_LOG_FILE}" 2>&1 &
pid=$!
printf '%s\n%s\n' "$pid" "vllm" > "${VLLM_PID_FILE}"
info "已启动（pid ${pid}），日志：${VLLM_LOG_FILE}"

section "2. 等待就绪（加载模型 + 编译 kernel，可能需要几分钟）"
if ! wait_for_http "http://${VLLM_HOST}:${VLLM_PORT}/v1/models" "vLLM" "${VLLM_STARTUP_TIMEOUT}"; then
  err "最近日志："
  tail -n 30 "${VLLM_LOG_FILE}" 2>/dev/null | sed 's/^/    /'
  exit 1
fi

ok "vLLM Model Service 已就绪"
curl -fsS "http://${VLLM_HOST}:${VLLM_PORT}/v1/models" | sed 's/^/    /'
echo
info "下一步：bash scripts/model-service/healthcheck_vllm.sh --probe"
