#!/usr/bin/env bash
# vLLM Model Service 健康检查。
#
#     bash scripts/model-service/healthcheck_vllm.sh            # 进程 + /v1/models
#     bash scripts/model-service/healthcheck_vllm.sh --probe    # 追加一次最小推理
#
# --probe 会向本地模型发一条最小 chat completion（max_tokens=8），
# 是 GPU 阶段"模型真的能推理"的第一手验证。无卡模式下会失败，属预期。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/model-service/_common.sh
. ./_common.sh

PROBE=0
[ "${1:-}" = "--probe" ] && PROBE=1

section "进程"
if is_running; then
  ok "vLLM 运行中（pid $(running_pid)）"
else
  err "vLLM 未运行 —— 启动：bash scripts/model-service/start_vllm.sh"
  exit 1
fi

section "HTTP /v1/models"
if http_ok "http://${VLLM_HOST}:${VLLM_PORT}/v1/models" 5; then
  ok "GET /v1/models 正常"
  curl -fsS "http://${VLLM_HOST}:${VLLM_PORT}/v1/models" \
    | "${VLLM_VENV}/bin/python" -c "
import json, sys
d = json.load(sys.stdin)
for m in d.get('data', []):
    print('    served model:', m.get('id'))
" 2>/dev/null || true
else
  err "GET /v1/models 无响应（端口 ${VLLM_PORT}）"
  info "最近日志："
  tail -n 20 "${VLLM_LOG_FILE}" 2>/dev/null | sed 's/^/    /'
  exit 1
fi

if [ "$PROBE" -eq 1 ]; then
  section "最小推理探针（--probe）"
  resp="$(curl -fsS --max-time 120 "http://${VLLM_HOST}:${VLLM_PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\": \"${VLLM_SERVED_NAME}\", \"messages\": [{\"role\": \"user\", \"content\": \"Reply with the single word: pong\"}], \"max_tokens\": 8}")" \
    || { err "推理探针失败（见 ${VLLM_LOG_FILE}）"; exit 1; }
  echo "$resp" | "${VLLM_VENV}/bin/python" -c "
import json, sys
d = json.load(sys.stdin)
msg = d['choices'][0]['message']['content']
print('    model:', d.get('model'))
print('    reply:', repr(msg))
" || { err "响应解析失败: $resp"; exit 1; }
  ok "推理探针通过"
fi

echo
ok "健康检查通过"
