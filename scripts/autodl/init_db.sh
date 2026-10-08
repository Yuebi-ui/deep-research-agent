#!/usr/bin/env bash
# 初始化 SQLite 数据库。
#
#     bash scripts/autodl/init_db.sh
#
# 首次部署用**全新空库**，通过 Alembic 建表 —— 绝不复制本机的真实
# data/tasks.db（那份数据留在开发机上）。
#
# 幂等：库已存在且已是最新版本时什么也不做。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

load_env
require_venv

PY="$(python_bin)"
export APP_ENV CONFIG_PATH CHECKPOINTER_BACKEND

DB_PATH="${PROJECT_ROOT}/data/tasks.db"

section "1. 检查现有数据库"
if [ -f "$DB_PATH" ]; then
  SIZE="$(du -h "$DB_PATH" | cut -f1)"
  warn "已存在数据库：data/tasks.db（${SIZE}）"
  current="$("$PY" - <<'PY' 2>/dev/null || echo "?"
import sqlite3
try:
    c = sqlite3.connect("data/tasks.db")
    print(c.execute("select version_num from alembic_version").fetchone()[0])
except Exception:
    print("未纳入迁移管理")
PY
)"
  info "当前 revision: ${current}"
  info "将执行 alembic upgrade head（只做增量，不删数据）"
else
  ok "无现有数据库 —— 将创建全新的空库"
fi

mkdir -p "${PROJECT_ROOT}/data"

section "2. 执行 alembic upgrade head"
cd "$PROJECT_ROOT"
if "$PY" -m alembic upgrade head 2>&1 | sed 's/^/    /'; then
  ok "迁移完成"
else
  die "alembic 失败。常见原因：
  - CONFIG_PATH 指向的 config.yml 不存在 → 先跑 setup.sh
  - config.yml 里 database.sqlite.path 指向的目录不可写
  - 见 docs/AUTODL_SERVER_V1_DEPLOYMENT_GUIDE.md 的「常见错误」一节"
fi

section "3. 校验结果"
"$PY" - <<'PY'
import sqlite3, sys
conn = sqlite3.connect("data/tasks.db")
tables = sorted(r[0] for r in conn.execute("select name from sqlite_master where type='table'"))
rev = conn.execute("select version_num from alembic_version").fetchone()[0]
n = conn.execute("select count(*) from tasks").fetchone()[0]
conn.close()

print(f"    表        : {tables}")
print(f"    revision  : {rev}")
print(f"    任务数    : {n}")

expected = {"tasks", "task_reviews", "alembic_version"}
missing = expected - set(tables)
if missing:
    print(f"    ✗ 缺少表: {missing}")
    sys.exit(1)
print("    ✓ schema 正确")
PY

ok "数据库就绪：data/tasks.db"
echo
echo "下一步：bash scripts/autodl/start_all.sh"
