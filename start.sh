#!/usr/bin/env bash
# 采购报价对比Agent 本地一键启动：后端（FastAPI/uvicorn）+ 前端（Vite dev server）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"

API_ONLY=0
WEB_ONLY=0
LOCAL_ONLY=0
DO_INSTALL=1
RELOAD=0
API_PORT=""
WEB_PORT=""

usage() {
  cat <<'EOF'
用法：./start.sh [选项]

  无参数              启动后端 + 前端（Ctrl+C 一起停止）
                      前端监听 0.0.0.0，同一网络的其它电脑/手机可用本机 IP 访问
  --local             仅本机可访问（前端只监听 127.0.0.1）
  --reload            后端热重载（改后端代码自动重启）
  --no-install        跳过依赖安装检查
  --api-only          只启动后端
  --web-only          只启动前端
  --api-port <端口>   后端端口（默认取 backend/.env 的 PORT，即 8002）
  --web-port <端口>   前端端口（默认取 frontend/.env 的 VITE_PORT，即 8003）
  -h, --help          显示本帮助

端口默认读 backend/.env 的 HOST/PORT 与 frontend/.env 的 VITE_PORT；
日志同时打印到终端（前缀 [api] / [web]）并落盘到 logs/backend.log、logs/frontend.log。
远程访问：前端请求同源的 /api，由 dev server 代理到后端，所以别人只需访问
  http://<本机局域网IP>:<前端端口>/  即可，后端始终只监听 127.0.0.1，无需对外开放。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --api-only) API_ONLY=1 ;;
    --web-only) WEB_ONLY=1 ;;
    --local) LOCAL_ONLY=1 ;;
    --no-install) DO_INSTALL=0 ;;
    --reload) RELOAD=1 ;;
    --api-port)
      API_PORT="${2:-}"
      if [ -z "$API_PORT" ]; then echo "✗ --api-port 需要一个端口号" >&2; exit 2; fi
      shift
      ;;
    --web-port)
      WEB_PORT="${2:-}"
      if [ -z "$WEB_PORT" ]; then echo "✗ --web-port 需要一个端口号" >&2; exit 2; fi
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "✗ 未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$API_ONLY" = 1 ] && [ "$WEB_ONLY" = 1 ]; then
  echo "✗ --api-only 与 --web-only 不能同时使用" >&2
  exit 2
fi

# ---- 输出工具 ----
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_API=$'\033[36m'; C_WEB=$'\033[35m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'
else
  C_RESET=''; C_API=''; C_WEB=''; C_OK=''; C_ERR=''
fi
info() { printf '%s\n' "$*"; }
ok() { printf '%s✓ %s%s\n' "$C_OK" "$*" "$C_RESET"; }
die() { printf '%s✗ %s%s\n' "$C_ERR" "$*" "$C_RESET" >&2; exit 1; }

# 读 .env 单个键（不 source，避免执行文件内容；去掉首尾引号）
env_value() {
  if [ ! -f "$1" ]; then return 0; fi
  sed -n "s/^[[:space:]]*$2=//p" "$1" | head -n 1 | sed "s/^[\"']//; s/[\"']\$//"
}

# 取本机局域网 IPv4（macOS：优先 en0，其次默认路由网卡；Linux：hostname -I 兜底）
lan_ip() {
  for iface in en0 en1 en2; do
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    if [ -n "$ip" ]; then printf '%s' "$ip"; return 0; fi
  done
  iface="$(route -n get default 2>/dev/null | sed -n 's/.*interface: //p' || true)"
  if [ -n "$iface" ]; then
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    if [ -n "$ip" ]; then printf '%s' "$ip"; return 0; fi
  fi
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  printf '%s' "$ip"
}

# ---- 前置检查 ----
if ! command -v uv >/dev/null 2>&1; then
  die "找不到 uv（后端依赖管理）。安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
fi
if [ "$WEB_ONLY" = 0 ] && ! command -v pnpm >/dev/null 2>&1; then
  die "找不到 pnpm（前端依赖管理）。安装：brew install pnpm"
fi
if ! command -v curl >/dev/null 2>&1; then
  die "找不到 curl（用于启动就绪探测）"
fi
if [ ! -f "$ROOT/backend/app/main.py" ]; then
  die "在 $ROOT 下找不到 backend/app/main.py，请把脚本放在项目根目录"
fi

# ---- .env 引导（缺失时从 .env.example 复制，沿用示例里的端口等默认值）----
for target in backend frontend; do
  if [ ! -f "$ROOT/$target/.env" ] && [ -f "$ROOT/$target/.env.example" ]; then
    cp "$ROOT/$target/.env.example" "$ROOT/$target/.env"
    info "→ 已从 $target/.env.example 生成 $target/.env"
  fi
done

API_HOST="$(env_value "$ROOT/backend/.env" HOST)"; API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-$(env_value "$ROOT/backend/.env" PORT)}"; API_PORT="${API_PORT:-8002}"
WEB_PORT="${WEB_PORT:-$(env_value "$ROOT/frontend/.env" VITE_PORT)}"; WEB_PORT="${WEB_PORT:-8003}"
# 绑定 0.0.0.0 时用回环地址访问/探测，否则用实际绑定地址
case "$API_HOST" in
  0.0.0.0) API_ADDR="127.0.0.1" ;;
  *) API_ADDR="$API_HOST" ;;
esac

# ---- 端口占用检查 ----
port_holder() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
}

check_port_free() { # 端口 服务名 参数名
  holder="$(port_holder "$1")"
  if [ -n "$holder" ]; then
    die "${2}端口 ${1} 已被占用（PID ${holder}）。先停止它：kill ${holder}
  或换端口：./start.sh ${3} <其它端口>"
  fi
}

if [ "$WEB_ONLY" = 0 ]; then check_port_free "$API_PORT" "后端" "--api-port"; fi
if [ "$API_ONLY" = 0 ]; then check_port_free "$WEB_PORT" "前端" "--web-port"; fi

# ---- 依赖 ----
if [ "$DO_INSTALL" = 1 ]; then
  if [ ! -d "$ROOT/backend/.venv" ]; then
    info "→ 安装后端依赖（uv sync）…"
    (cd "$ROOT/backend" && uv sync)
  fi
  if [ "$API_ONLY" = 0 ] && [ ! -d "$ROOT/frontend/node_modules" ]; then
    info "→ 安装前端依赖（pnpm install）…"
    (cd "$ROOT/frontend" && pnpm install)
  fi
fi

# ---- 前端访问后端 ----
# 默认同源：前端请求相对路径 /api，由 vite dev server 代理到后端。
# 这样从 127.0.0.1、局域网 IP、域名访问都可用，不写死 IP、也没有跨域问题。
export VITE_API_PORT="$API_PORT"
# 只有 --local 时才限制成本机可访问
if [ "$LOCAL_ONLY" = 1 ]; then export VITE_HOST="127.0.0.1"; else export VITE_HOST="0.0.0.0"; fi
env_base="$(env_value "$ROOT/frontend/.env" VITE_API_BASE)"
if [ -n "${VITE_API_BASE:-}" ]; then
  info "→ 前端直连后端 ${VITE_API_BASE}（由环境变量 VITE_API_BASE 指定）"
elif [ -n "$env_base" ]; then
  case "$env_base" in
    # 环回地址只有「打开页面那台机器」能用，跨机访问会打到对方自己的本机 → 改走同源代理
    http://127.0.0.1:*|http://localhost:*|http://0.0.0.0:*)
      info "→ 前端改用 dev server 代理 /api → 127.0.0.1:${API_PORT}（frontend/.env 里的 ${env_base} 仅本机可用，别人打开页面会失效）"
      ;;
    *)
      VITE_API_BASE="$env_base"
      info "→ 前端直连后端 ${VITE_API_BASE}（取自 frontend/.env）"
      ;;
  esac
fi
# 显式导出（空串即同源代理）：Vite 的 process.env 优先于 frontend/.env，
# 否则别人打开页面时仍会用 .env 里写死的 127.0.0.1
VITE_API_BASE="${VITE_API_BASE:-}"
export VITE_API_BASE
# vite.config.ts 只读 process.env.VITE_PORT，不读 frontend/.env，所以这里必须显式导出
export VITE_PORT="$WEB_PORT"

mkdir -p "$LOG_DIR"
PIDS=""
SHUTTING_DOWN=0
exec 3>&2 # 保留原始 stderr，供日志管道使用（进程替换子 shell 会把自己的作业提示静音）

collect_tree() { # 后序输出：先后代、再自身；pgrep -P 按 PID 定位，不做名称匹配
  pid="$1"
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do collect_tree "$child"; done
  printf '%s\n' "$pid"
}

cleanup() {
  trap - EXIT INT TERM
  trap '' INT TERM # 收尾期间忽略重复的 Ctrl+C / kill，避免清到一半被打断留下孤儿进程
  SHUTTING_DOWN=1
  exec 9>&2 2>/dev/null # 静音这段窗口内 bash 打印的作业终止提示
  if [ -z "$PIDS" ]; then
    exec 2>&9; exec 9>&-
    return 0
  fi
  printf '\n→ 正在停止服务…\n'
  # 先把整棵树（含 uv/pnpm 派生的子孙）快照下来再统一 TERM。
  # 只按 $PIDS 判定存活是不够的：uvicorn --reload 的 reloader 会偶发卡在
  # 文件扫描循环里，父进程死了它仍占着端口，必须能追到并 KILL 掉。
  ALL=""
  for pid in $PIDS; do ALL="$ALL $(collect_tree "$pid")"; done
  for pid in $ALL; do kill "$pid" 2>/dev/null || true; done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive=0
    for pid in $ALL; do
      if kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    if [ "$alive" = 0 ]; then break; fi
    sleep 0.5
  done
  for pid in $ALL; do
    if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" 2>/dev/null || true; fi
  done
  for pid in $PIDS; do # 再快照一次，兜住 TERM 期间新派生出来的进程
    for child in $(collect_tree "$pid" 2>/dev/null || true); do
      if kill -0 "$child" 2>/dev/null; then kill -9 "$child" 2>/dev/null || true; fi
    done
  done
  exec 2>&9
  exec 9>&-
  ok "服务已停止"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ---- 启动 ----
API_PID=""
WEB_PID=""

if [ "$WEB_ONLY" = 0 ]; then
  reload_arg=""
  if [ "$RELOAD" = 1 ]; then reload_arg="--reload"; fi
  info "→ 启动后端：uvicorn app.main:app --host $API_HOST --port $API_PORT $reload_arg"
  (cd "$ROOT/backend" && exec uv run uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" $reload_arg 3>&-) \
    > >( { exec 2>/dev/null; awk -v p="${C_API}[api]${C_RESET}" '{ print p " " $0; fflush() }' | tee "$LOG_DIR/backend.log" 2>&3; } ) 2>&1 &
  API_PID=$!
  PIDS="$API_PID"
fi

if [ "$API_ONLY" = 0 ]; then
  if [ -n "$VITE_API_BASE" ]; then
    info "→ 启动前端：vite dev server @ ${WEB_PORT}（API 直连 ${VITE_API_BASE}）"
  else
    info "→ 启动前端：vite dev server @ ${WEB_PORT}（API 同源代理 → 127.0.0.1:${API_PORT}）"
  fi
  # --strictPort：端口被占用时直接报错退出，避免 vite 静默换端口导致提示的地址不对
  (cd "$ROOT/frontend" && exec pnpm dev --strictPort 3>&-) \
    > >( { exec 2>/dev/null; awk -v p="${C_WEB}[web]${C_RESET}" '{ print p " " $0; fflush() }' | tee "$LOG_DIR/frontend.log" 2>&3; } ) 2>&1 &
  WEB_PID=$!
  PIDS="$PIDS $WEB_PID"
fi

# ---- 就绪探测（失败时打印日志尾巴）----
wait_http() { # URL 进程PID 名称 超时秒 日志文件
  url="$1"; pid="$2"; name="$3"; limit="$4"; log="$5"; waited=0
  while [ "$waited" -lt "$limit" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '%s✗ %s启动失败：进程已退出，见 logs/%s%s\n' "$C_ERR" "$name" "$log" "$C_RESET" >&2
      return 1
    fi
    if curl -fsS --noproxy '*' --max-time 2 -o /dev/null "$url" 2>/dev/null; then
      return 0
    fi
    waited=$((waited + 1))
    sleep 1
  done
  printf '%s✗ %s在 %s 秒内未就绪：%s%s\n' "$C_ERR" "$name" "$limit" "$url" "$C_RESET" >&2
  return 1
}

FAILED=0
if [ -n "$API_PID" ]; then
  if wait_http "http://$API_ADDR:$API_PORT/health" "$API_PID" "后端" 90 "backend.log"; then
    ok "后端就绪：http://$API_ADDR:$API_PORT/health"
  else
    FAILED=1
  fi
fi
if [ "$FAILED" = 0 ] && [ -n "$WEB_PID" ]; then
  if wait_http "http://127.0.0.1:$WEB_PORT/" "$WEB_PID" "前端" 90 "frontend.log"; then
    ok "前端就绪：http://127.0.0.1:$WEB_PORT/"
  else
    FAILED=1
  fi
fi

if [ "$FAILED" = 1 ]; then
  info "→ 末尾日志（完整日志见 logs/）："
  if [ -n "$API_PID" ]; then tail -n 20 "$LOG_DIR/backend.log" 2>/dev/null || true; fi
  if [ -n "$WEB_PID" ]; then tail -n 20 "$LOG_DIR/frontend.log" 2>/dev/null || true; fi
  exit 1
fi

printf '\n'
ok "启动完成"
printf '\n'
if [ -n "$WEB_PID" ]; then info "  前端页面   http://127.0.0.1:${WEB_PORT}/"; fi
if [ -n "$WEB_PID" ] && [ "$LOCAL_ONLY" = 0 ]; then
  share_ip="$(lan_ip)"
  if [ -n "$share_ip" ]; then
    info "  共享访问   http://${share_ip}:${WEB_PORT}/   ← 发给同事，同一网络即可打开"
  else
    info "  共享访问   未检测到局域网 IP（查本机 IP：ipconfig getifaddr en0）"
  fi
fi
if [ -n "$API_PID" ]; then info "  后端接口   http://${API_ADDR}:${API_PORT}/docs"; fi
if [ -n "$API_PID" ] && [ -n "$WEB_PID" ]; then
  info "  日志       logs/backend.log  logs/frontend.log"
elif [ -n "$API_PID" ]; then
  info "  日志       logs/backend.log"
else
  info "  日志       logs/frontend.log"
fi
info "  提示       LLM 网关默认 http://localhost:18080/v1（可用环境变量或 backend/.env 覆盖）"
info "             按 Ctrl+C 停止全部服务"
printf '\n'

# ---- 守护：任一服务退出即整体收尾 ----
# 终端里的 Ctrl+C 是直接打到整个前台进程组上的，服务自己会先开始优雅退出。
# 这里额外做两件事：
#  1) 用 wait 代替 sleep 空转：bash 在内建 wait 期间能立即处理信号 trap，
#     从而在服务退出之前就跑收尾逻辑，不会误报“进程已退出”；
#  2) 保底地按退出状态判断：若进程是被信号结束的（status > 128），视为正常停止。
while :; do
  if [ "$SHUTTING_DOWN" = 1 ]; then exit 0; fi
  for pid in $PIDS; do
    if ! kill -0 "$pid" 2>/dev/null; then
      st=0
      wait "$pid" 2>/dev/null || st=$?
      # 130/143 = 被 SIGINT/SIGTERM 结束，是外部停止而非崩溃；其它非 0（如 137/139）仍按异常上报
      if [ "$st" -eq 130 ] || [ "$st" -eq 143 ]; then
        exit "$st"
      fi
      if [ "$pid" = "$API_PID" ]; then
        die "后端进程已退出（见 logs/backend.log）"
      fi
      if [ "$pid" = "$WEB_PID" ]; then
        die "前端进程已退出（见 logs/frontend.log）"
      fi
    fi
  done
  sleep 1 &
  wait $! 2>/dev/null || true
done
