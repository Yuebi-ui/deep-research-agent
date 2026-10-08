#!/usr/bin/env bash
# 重启本地 vLLM Model Service。
#
#     bash scripts/model-service/restart_vllm.sh

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash ./stop_vllm.sh
bash ./start_vllm.sh
