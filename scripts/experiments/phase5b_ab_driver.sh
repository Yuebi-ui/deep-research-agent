#!/usr/bin/env bash
# Phase 5B A/B 驱动：CONTROL（串行）vs TREATMENT（seed-lite speculative，productionized）。
#
#     bash scripts/experiments/phase5b_ab_driver.sh --arm control|treatment|both [--queries q1 z2] [--round r2] [--review-action approve|revise,approve]
#
# 与 Phase 5A 的差别：
#   - 工作负载扩展为 8 个查询：q1–q4（与 5A 相同，保证可比性）+ z1–z4（中文，
#     取自 tests/eval_dataset.json，覆盖 5A 报告明确未覆盖的多语言盲区）；
#   - 支持 --review-action 序列（如 revise,approve）以在真实 runtime 上验证
#     reject → regenerate → accept 路径（run_baseline 会按序消费 review 动作）；
#   - run-id 前缀 p5b-。
#
# 每个 arm：
#   1) 停 API+Worker（改过源码 → preflight 的 service_freshness 要求重启）
#   2) 等 TIME_WAIT（stop.sh 后立刻 start 会误报端口占用）
#   3) 用对应开关重启服务，并**核验 worker 进程环境**确实带上开关
#   4) 顺序跑 workload（run_baseline：preflight + fingerprint + validity 全套）
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
ROOT="$(pwd)"

ARM=""
ROUND=""
QUERIES=()
REVIEW_ACTION="approve"
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) ARM="$2"; shift 2 ;;
    --queries) shift; while [ $# -gt 0 ] && [[ "${1:-}" != --* ]]; do QUERIES+=("$1"); shift; done ;;
    --round) ROUND="$2"; shift 2 ;;
    --review-action) REVIEW_ACTION="$2"; shift 2 ;;
    *) echo "unknown arg $1"; exit 2 ;;
  esac
done
[ -z "$ARM" ] && ARM="both"
[ ${#QUERIES[@]} -eq 0 ] && QUERIES=(q1 q2 q3 q4 z1 z2 z3 z4)

declare -A QUERY_TEXT=(
  [q1]="What is LangGraph and what are its main use cases?"
  [q2]="Which organization maintains the LangGraph library, and what is its current stable major version?"
  [q3]="Compare LangGraph, CrewAI, and AutoGen for building multi-agent LLM applications: what are the key architectural differences and trade-offs?"
  [q4]="How should a small engineering team decide whether to adopt an agent framework like LangGraph instead of building directly on an LLM API?"
  [z1]="2025年AI Agent框架的发展趋势是什么？"
  [z2]="什么是检索增强生成（RAG）？它如何减少大模型幻觉？"
  [z3]="比较 OpenAI 和 Anthropic 在 AI Safety 方面的做法"
  [z4]="大模型推理优化的最新技术有哪些？"
)
declare -A QUERY_KIND=([q1]=broad-en [q2]=narrow-factual-en [q3]=synthesis-en [q4]=scope-open-en
                      [z1]=broad-zh [z2]=definitional-zh [z3]=comparison-zh [z4]=survey-zh)

run_arm() {
  local arm="$1" flag="$2" tag="$3"
  echo "=========== ARM=$arm flag=${flag:-<unset>} review-action=$REVIEW_ACTION ==========="
  bash scripts/autodl/stop.sh >/dev/null 2>&1
  sleep 75                                     # TIME_WAIT

  if [ -n "$flag" ]; then
    DR_SEED_LITE_SPECULATIVE="$flag" bash scripts/autodl/start_all.sh 2>&1 | tail -3
  else
    # Phase 5B 定版后默认即 on：control 臂必须**显式** off，否则两臂同语义
    DR_SEED_LITE_SPECULATIVE=off bash scripts/autodl/start_all.sh 2>&1 | tail -3
  fi

  # 核验开关真的进了 worker 进程环境（不能只看启动脚本）
  local wpid; wpid="$(head -1 .run/worker.pid)"
  echo "[check] worker pid=$wpid DR_SEED_LITE_SPECULATIVE=$(tr '\0' '\n' < "/proc/$wpid/environ" | grep '^DR_SEED_LITE_SPECULATIVE=' || echo '<unset>')"

  for q in "${QUERIES[@]}"; do
    local rid="p5b-${tag}-${q}${ROUND:+-$ROUND}"
    echo "--- [$rid] ${QUERY_KIND[$q]}: ${QUERY_TEXT[$q]:0:60}..."
    ALLOW_LIVE_EXTERNAL_APIS=true .venv/bin/python scripts/run_baseline.py \
      --run-id "$rid" --experiment-id phase5b-seed-lite-prod --variant "$tag" \
      --experiment-kind experiment --query "${QUERY_TEXT[$q]}" \
      --review-action "$REVIEW_ACTION" --force \
      > "/tmp/${rid}.log" 2>&1
    echo "    exit=$? $(grep -E '^latency|^validity|^reliab' "/tmp/${rid}.log" | tr '\n' ' ')"
  done
}

[ "$ARM" = "control" ]   && run_arm control   ""   ctl
[ "$ARM" = "treatment" ] && run_arm treatment on   trt
if [ "$ARM" = "both" ]; then
  run_arm control   "" ctl
  run_arm treatment on trt
fi

echo "=========== 全部完成；恢复生产默认（未设置 env = 默认 on）==========="
bash scripts/autodl/stop.sh >/dev/null 2>&1
sleep 75
bash scripts/autodl/start_all.sh 2>&1 | tail -3
