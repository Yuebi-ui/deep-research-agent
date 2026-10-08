#!/usr/bin/env bash
# Phase 5A A/B 驱动：CONTROL（串行）vs TREATMENT（seed-lite speculative）。
#
#     bash scripts/experiments/phase5a_ab_driver.sh --arm control|treatment|both [--queries q1 q2] [--round r2]
#
# --round：给 run_id 加后缀（如 p5a-ctl-q1-r2），用于**多轮重复**而不覆盖上一轮产物
#
# 每个 arm：
#   1) 停 API+Worker（改过源码 → preflight 的 service_freshness 要求重启）
#   2) 等 TIME_WAIT（stop.sh 后立刻 start 会误报端口占用，见交接文档踩坑 #5）
#   3) 用对应开关重启服务，并**核验 worker 进程环境**确实带上开关
#   4) 顺序跑 workload（run_baseline：preflight + fingerprint + validity 全套）
#
# 只改实验开关，其余（模型/thinking/context budget/search/evaluator）完全一致。
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ROOT="$(pwd)"

ARM=""
ROUND=""
QUERIES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) ARM="$2"; shift 2 ;;
    --queries) shift; while [ $# -gt 0 ] && [[ "${1:-}" != --* ]]; do QUERIES+=("$1"); shift; done ;;
    --round) ROUND="$2"; shift 2 ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
done
[ -z "$ARM" ] && ARM="both"
[ ${#QUERIES[@]} -eq 0 ] && QUERIES=(q1 q2 q3 q4)

declare -A QUERY_TEXT=(
  [q1]="What is LangGraph and what are its main use cases?"
  [q2]="Which organization maintains the LangGraph library, and what is its current stable major version?"
  [q3]="Compare LangGraph, CrewAI, and AutoGen for building multi-agent LLM applications: what are the key architectural differences and trade-offs?"
  [q4]="How should a small engineering team decide whether to adopt an agent framework like LangGraph instead of building directly on an LLM API?"
)
declare -A QUERY_KIND=([q1]=broad [q2]=narrow [q3]=synthesis [q4]=scope-open)

run_arm() {
  local arm="$1" flag="$2" tag="$3"
  echo "=========== ARM=$arm flag=${flag:-<unset>} ==========="
  bash scripts/autodl/stop.sh >/dev/null 2>&1
  sleep 75                                     # TIME_WAIT（踩坑 #5）

  if [ -n "$flag" ]; then
    DR_SEED_LITE_SPECULATIVE="$flag" bash scripts/autodl/start_all.sh 2>&1 | tail -3
  else
    # Phase 5B 定版后开关默认 on：control（串行）臂必须显式 off，
    # 否则重跑本脚本会得到两臂同语义的假结果。
    DR_SEED_LITE_SPECULATIVE=off bash scripts/autodl/start_all.sh 2>&1 | tail -3
  fi

  # 核验开关真的进了 worker 进程环境（不能只看启动脚本）
  local wpid; wpid="$(head -1 .run/worker.pid)"
  echo "[check] worker pid=$wpid DR_SEED_LITE_SPECULATIVE=$(tr '\0' '\n' < "/proc/$wpid/environ" | grep '^DR_SEED_LITE_SPECULATIVE=' || echo '<unset>')"

  for q in "${QUERIES[@]}"; do
    local rid="p5a-${tag}-${q}${ROUND:+-$ROUND}"
    echo "--- [$rid] ${QUERY_KIND[$q]}: ${QUERY_TEXT[$q]:0:60}..."
    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/run_baseline.py \
      --run-id "$rid" --experiment-id phase5a-seed-lite --variant "$tag" \
      --experiment-kind experiment --query "${QUERY_TEXT[$q]}" --force \
      > "/tmp/${rid}.log" 2>&1
    echo "    exit=$? $(grep -E '^latency|^validity' "/tmp/${rid}.log" | tr '\n' ' ')"
  done
}

[ "$ARM" = "control" ]   && run_arm control   ""   ctl
[ "$ARM" = "treatment" ] && run_arm treatment on   trt
if [ "$ARM" = "both" ]; then
  run_arm control   "" ctl
  run_arm treatment on trt
fi

echo "=========== 全部完成；恢复默认（flag off）==========="
bash scripts/autodl/stop.sh >/dev/null 2>&1
sleep 75
bash scripts/autodl/start_all.sh 2>&1 | tail -3
