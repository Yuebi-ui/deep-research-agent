#!/usr/bin/env bash
# 查看 API / Worker / Redis 状态与健康检查。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

load_env

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"

redis_do() {
  redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "$@" 2>/dev/null || true
}

section "服务进程"
for name in redis api worker; do
  if is_running "$name"; then
    line="$(read_pidfile "$name" || true)"
    pid="${line%% *}"
    marker="${line#* }"
    [ "$marker" = "$pid" ] && marker=""
    ok "$name 运行中（pid ${pid}${marker:+  $marker}）"
  else
    err "$name 未运行"
  fi
done

section "Redis"
ping_out="$(redis_do ping)"
if [ "$ping_out" = "PONG" ]; then
  ok "PONG"
  raw_modules="$(redis_do MODULE LIST)"
  found=""
  if printf '%s' "$raw_modules" | grep -qi "search"; then found="${found}search "; fi
  if printf '%s' "$raw_modules" | grep -qiE "rejson|json"; then found="${found}ReJSON "; fi
  if [ -n "$found" ]; then
    ok "模块: ${found}"
  else
    err "未检测到 RediSearch / RedisJSON —— 请使用 Redis Stack"
  fi
  info "job 队列长度: $(redis_do XLEN dr:jobs)"
  info "claim 数量:   $(redis_do --scan --pattern 'dr:claim:*' | wc -l | tr -d ' ')"
else
  err "无响应（${REDIS_HOST}:${REDIS_PORT}）"
fi

section "API"
if http_ok "http://127.0.0.1:${API_PORT}/api/health"; then
  ok "GET /api/health 正常"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 5 "http://127.0.0.1:${API_PORT}/api/health" 2>/dev/null | sed 's/^/    /' || true
    echo
  fi
else
  err "GET /api/health 无响应（端口 ${API_PORT}）"
fi

section "数据库"
if [ -f "${PROJECT_ROOT}/data/tasks.db" ]; then
  "$(python_bin)" - <<'PY' 2>/dev/null || warn "无法读取数据库（可能尚未初始化）"
import sqlite3
try:
    c = sqlite3.connect("data/tasks.db")
    print("    revision  :", c.execute("select version_num from alembic_version").fetchone()[0])
    print("    任务总数  :", c.execute("select count(*) from tasks").fetchone()[0])
    rows = list(c.execute("select status, count(*) from tasks group by status order by 2 desc"))
    for st, n in rows:
        print(f"      {st:16s} {n}")
    c.close()
except Exception as exc:
    print("    读取失败:", exc)
PY
else
  warn "数据库不存在（先跑 init_db.sh）"
fi

section "最近日志（各 3 行）"
for name in api worker; do
  f="${LOG_DIR}/${name}.log"
  if [ -f "$f" ]; then
    echo "  --- ${name} ---"
    tail -n 3 "$f" 2>/dev/null | sed 's/^/    /' || true
  fi
done
