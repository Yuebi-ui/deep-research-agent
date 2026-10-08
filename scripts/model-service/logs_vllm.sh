#!/usr/bin/env bash
# 查看 vLLM Model Service 日志。
#
#     bash scripts/model-service/logs_vllm.sh          # 最近 100 行
#     bash scripts/model-service/logs_vllm.sh -f       # 实时跟踪（Ctrl+C 退出）
#     LINES=500 bash scripts/model-service/logs_vllm.sh

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/model-service/_common.sh
. ./_common.sh

LINES="${LINES:-100}"
FOLLOW=""
[ "${1:-}" = "-f" ] && FOLLOW="-f"

if [ ! -f "$VLLM_LOG_FILE" ]; then
  info "日志文件尚不存在：${VLLM_LOG_FILE}"
  info "（vLLM 还没有被启动过）"
  exit 0
fi

info "日志：${VLLM_LOG_FILE}"
tail -n "$LINES" $FOLLOW "$VLLM_LOG_FILE"
