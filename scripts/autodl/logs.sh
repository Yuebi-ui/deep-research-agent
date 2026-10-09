#!/usr/bin/env bash
# 查看服务日志。
#
#     bash scripts/autodl/logs.sh api
#     bash scripts/autodl/logs.sh worker -f
#     bash scripts/autodl/logs.sh all

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

TARGET="${1:-all}"
shift || true
TAIL_ARGS=(-n "${LINES:-100}")
for a in "$@"; do TAIL_ARGS+=("$a"); done

show() {
  local f="${LOG_DIR}/$1.log"
  if [ -f "$f" ]; then
    echo "===== $1（$f）====="
    tail "${TAIL_ARGS[@]}" "$f"
    echo
  else
    warn "无日志：$f"
  fi
}

case "$TARGET" in
  all) for n in redis api worker; do show "$n"; done ;;
  redis|api|worker) show "$TARGET" ;;
  *) die "用法: logs.sh [all|api|worker|redis] [-f] [LINES=200]" ;;
esac
