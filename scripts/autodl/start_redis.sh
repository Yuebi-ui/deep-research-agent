#!/usr/bin/env bash
# 启动 Redis Stack。
#
#     bash scripts/autodl/start_redis.sh            # 检测 → 报告 → 给出下一步
#     bash scripts/autodl/start_redis.sh --yes      # 允许脚本自动执行安装
#
# ## 为什么必须是 Redis Stack，而不是普通 Redis
#
# 本项目的三项能力都依赖 Redis：
#     job 队列 / 事件流 / LangGraph checkpointer
#
# 其中 checkpointer 用的是 `langgraph-checkpoint-redis`，它需要
# **RediSearch 与 RedisJSON** 两个模块 —— 普通 Redis 镜像/二进制没有它们，
# 会在 checkpointer 初始化时直接失败。所以本脚本最后一定会校验模块。
#
# ## 按可用性依次尝试
#
#   1. 已有可用的 Redis Stack 在跑     → 直接复用
#   2. PATH 里有 redis-stack-server    → 直接启动
#   3. 有可用的 Docker                 → 用官方镜像起一个
#   4. Debian/Ubuntu + 有 sudo + 联网  → 通过官方 APT 仓库安装
#   5. 都不行                          → 给出明确的手动步骤并退出
#
# AutoDL 普通实例常见情况是 **不能用 Docker**，因此第 4 条是主力路径。

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.."
PROJECT_ROOT="$(pwd)"
# shellcheck source=scripts/autodl/_common.sh
. "${PROJECT_ROOT}/scripts/autodl/_common.sh"

load_env

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6379}"
AUTO_YES=0
[ "${1:-}" = "--yes" ] && AUTO_YES=1

REDIS_DATA_DIR="${PROJECT_ROOT}/data/redis"
mkdir -p "$REDIS_DATA_DIR"

redis_ping() {
  command -v redis-cli >/dev/null 2>&1 \
    && redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG
}

redis_has_modules() {
  # Check BOTH Redis Search and RedisJSON; matching only one is insufficient.
  local modules
  command -v redis-cli >/dev/null 2>&1 || return 1
  modules="$(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" MODULE LIST 2>/dev/null)" || return 1
  printf '%s\n' "$modules" | grep -qi 'search' \
    && printf '%s\n' "$modules" | grep -qiE 'rejson|json'
}

# ----------------------------------------------------------------------

section "1. 检查是否已有可用的 Redis Stack"
if redis_ping; then
  if redis_has_modules; then
    ok "已有 Redis Stack 在 ${REDIS_HOST}:${REDIS_PORT} 运行，直接复用"
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" MODULE LIST 2>/dev/null | head -6 | sed 's/^/    /'
    exit 0
  else
    err "${REDIS_HOST}:${REDIS_PORT} 上有一个 Redis，但**缺少 RediSearch/RedisJSON**"
    err "它不是 Redis Stack，checkpointer 会失败。"
    err "请先停掉它，或改用别的端口（export REDIS_PORT=6380 后重跑本脚本），"
    err "并把 config.yml 里的 redis.url 同步改掉。"
    exit 1
  fi
fi

section "2. 检查 PATH 中是否有 redis-stack-server"
if command -v redis-stack-server >/dev/null 2>&1; then
  ok "找到 redis-stack-server：$(command -v redis-stack-server)"
  info "启动中（数据目录 ${REDIS_DATA_DIR}）…"
  start_daemon "redis" "${LOG_DIR}/redis.log" "redis-stack-server" \
    redis-stack-server \
      --port "$REDIS_PORT" \
      --dir "$REDIS_DATA_DIR" \
      --save "" \
      --appendonly yes

  # Redis uses RESP, NOT HTTP. Probe redis-cli rather than curl / wait_for_http.
  for _ in 1 2 3 4 5; do
    if redis_ping && redis_has_modules; then
      ok "Redis Stack 已就绪"
      exit 0
    fi
    sleep 1
  done
  err "启动后仍未通过校验，请查看 ${LOG_DIR}/redis.log"
  exit 1
fi

section "3. 检查 Docker 是否可用"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "Docker 可用"
  info "用官方镜像启动 redis/redis-stack-server…"
  docker rm -f dr-redis-stack >/dev/null 2>&1 || true
  docker run -d --name dr-redis-stack \
    -p "${REDIS_PORT}:6379" \
    -v "${REDIS_DATA_DIR}:/data" \
    redis/redis-stack-server:latest >/dev/null \
    && ok "容器已启动（dr-redis-stack）"

  # Optional Redis-only Docker fallback. Do not treat an open port as proof
  # of the required Redis modules or as evidence of Docker-based GPU serving.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if redis_ping && redis_has_modules; then
      ok "Redis Stack 容器已就绪（Search + JSON 模块均可用）"
      echo
      echo "提示：容器由 Docker 管理，停止用 docker stop dr-redis-stack"
      exit 0
    fi
    sleep 1
  done
  err "Redis Stack 容器未通过 PING + 模块检查，请检查 docker logs dr-redis-stack"
  exit 1
else
  info "Docker 不可用 —— 这在 AutoDL 普通实例上是正常的，继续下一步"
fi

section "4. 尝试通过官方 APT 仓库安装 Redis Stack"

# --- 发行版代号：lsb_release 不一定存在，回退到 /etc/os-release ---
DISTRO_CODENAME=""
if command -v lsb_release >/dev/null 2>&1; then
  DISTRO_CODENAME="$(lsb_release -cs 2>/dev/null || true)"
fi
if [ -z "$DISTRO_CODENAME" ] && [ -r /etc/os-release ]; then
  DISTRO_CODENAME="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-}")"
fi

if ! command -v apt-get >/dev/null 2>&1; then
  info "无 apt-get —— 不是 Debian/Ubuntu 系，跳到第 5 节"
elif [ -z "$DISTRO_CODENAME" ]; then
  warn "无法确定发行版代号（lsb_release 缺失且 /etc/os-release 里没有 VERSION_CODENAME）"
  info "请人工确认后手动执行第 5 节列出的命令"
else
  info "检测到 Debian/Ubuntu 系，发行版代号: ${DISTRO_CODENAME}"

  echo "将执行以下命令（需要 sudo 权限）："
  echo
  cat <<APTCMD
  curl -fsSL https://packages.redis.io/gpg | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg
  sudo chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb ${DISTRO_CODENAME} main" | sudo tee /etc/apt/sources.list.d/redis.list
  sudo apt-get update
  sudo apt-get install -y redis-stack-server
APTCMD
  echo

  if [ "$AUTO_YES" -ne 1 ]; then
    info "未加 --yes，未自动安装。"
    echo "  可以直接让本脚本代劳："
    echo "      bash scripts/autodl/start_redis.sh --yes"
    exit 0
  fi

  info "--yes 已指定，开始安装…"

  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"

  KEYRING=/usr/share/keyrings/redis-archive-keyring.gpg
  LIST=/etc/apt/sources.list.d/redis.list

  # 关键：--batch --yes 让它非交互地覆盖已有 keyring。
  # 不加这两个参数时 gpg 会尝试打开 /dev/tty 询问，非交互环境直接失败。
  if ! curl -fsSL https://packages.redis.io/gpg \
       | ${SUDO} gpg --batch --yes --dearmor --output "$KEYRING" 2>/dev/null; then
    die "下载/写入 Redis GPG 公钥失败（网络受限，或 gpg 不可用）
  可改用部署指南 13.3 节的手工路径"
  fi
  ${SUDO} chmod 644 "$KEYRING" 2>/dev/null || true
  ok "GPG 公钥已写入"

  # 用 > 覆盖而非追加：避免上一次失败留下的 malformed entry 残留
  if ! echo "deb [signed-by=${KEYRING}] https://packages.redis.io/deb ${DISTRO_CODENAME} main" \
       | ${SUDO} tee "$LIST" >/dev/null; then
    die "写入 ${LIST} 失败"
  fi
  ok "APT 源已写入（覆盖模式，避免历史残留）"

  if ! ${SUDO} apt-get update -qq; then
    die "apt-get update 失败 —— 检查上面的报错，常见是 ${LIST} 内容格式不对"
  fi
  ok "apt 索引已更新"

  if ! ${SUDO} apt-get install -y redis-stack-server; then
    die "安装 redis-stack-server 失败"
  fi
  ok "redis-stack-server 已安装"

  echo
  info "重新执行本脚本以启动 Redis…"
  exec bash "${PROJECT_ROOT}/scripts/autodl/start_redis.sh"
fi

section "5. 无法自动安装"
cat <<'MANUAL'
当前环境既不满足自动安装条件，也没有可用的 Redis Stack。

可选的人工路径：

A) 若这台机器是 Ubuntu/Debian 且有 sudo
   执行上面第 4 节列出的 APT 命令，然后重跑本脚本。

B) 用官方二进制包（无 Docker）
   1. 在本机浏览器打开 https://redis.io/downloads/
   2. 选择 Redis Stack Server 的 Linux x86_64 压缩包下载
   3. 解压后把 bin 目录加进 PATH：
        tar -xzf redis-stack-server-*.tar.gz
        export PATH="$PWD/redis-stack-server-*/bin:$PATH"
   4. 重跑本脚本

C) 若你的 AutoDL 实例其实支持 Docker（部分镜像支持）
   直接：
        docker run -d --name dr-redis-stack -p 6379:6379 redis/redis-stack-server:latest

⚠️ 注意：网上有些"预编译 redis 二进制"是**普通 Redis**，不含
   RediSearch/RedisJSON，装上去会在 checkpointer 初始化时失败。
   本脚本最后会校验模块，校验不过就不会放行。
MANUAL

err "Redis Stack 未就绪 —— 它是本项目的硬依赖，无法跳过"
exit 1
