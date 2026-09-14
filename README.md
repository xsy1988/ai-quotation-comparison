# 采购报价对比Agent

上传多家供应商报价单（Excel / Word / 文字版 PDF / 扫描件图片），LLM 版面理解 + 脚本交叉验证解析，原子工艺映射后机械对比 + 顶部 AI 分析模块（表格化对比：含税单价/优势/劣势/风险/建议，支持子维度展开），支持就地修正、别名回流、新工艺决策与主数据自管理。前端为左侧菜单栏导航（发起报价对比 / 报价对比历史 / 报价单数据 / 供应商管理 / 基础数据维护）。

## 端口

| 服务 | 地址 | 登记 |
|---|---|---|
| 后端 API（FastAPI/uvicorn） | `127.0.0.1:8002` | port-manager：项目「采购报价对比Agent」service `api` |
| 前端开发服务器（Vite） | `0.0.0.0:8003`（本机 `127.0.0.1:8003`，局域网用本机 IP 访问） | port-manager：项目「采购报价对比Agent」service `frontend` |

端口已在项目 demo 端口管理器中登记（`docs/PORT_ALLOCATION_GUIDE.md`）。如需调整，先运行 `pm allocate` 申请新端口，再改 `backend/.env`（PORT/HOST）与 `frontend/.env`（VITE_PORT/VITE_API_PORT）。

## 启动

一条命令拉起前后端（推荐）：

```bash
./start.sh              # 后端 8002 + 前端 8003，Ctrl+C 一起停
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--reload` | 后端热重载（改后端代码自动重启） |
| `--no-install` | 跳过依赖安装检查 |
| `--api-only` / `--web-only` | 只启动后端 / 只启动前端 |
| `--api-port <端口>` / `--web-port <端口>` | 临时换端口（前端会自动指向新的后端端口） |
| `--local` | 只允许本机访问（前端绑定 `127.0.0.1`）；默认允许局域网/远程访问 |
| `-h, --help` | 帮助 |

脚本行为：`.env` 缺失时从 `.env.example` 生成 → 检查端口占用（被占用会提示占用 PID 与 kill 命令）→ 依赖缺失时自动 `uv sync` / `pnpm install` → 带 `[api]` / `[web]` 前缀把日志同时打到终端和 `logs/backend.log`、`logs/frontend.log` → 等 `/health` 与前端首页就绪后打印访问地址 → 任一服务退出或 Ctrl+C 时整组停止（含 `uv`/`pnpm` 派生的子进程，不会残留占端口）。

前端以 `--strictPort` 启动：端口被占用会立即报错退出（不会静默换成别的端口导致提示地址不对）。

前置：LLM 网关可达（默认 `http://localhost:18080/v1`，可用环境变量 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` 覆盖）。

手动启动（等价于脚本内部做的事）：

```bash
# 后端（端口/绑定读 backend/.env）
cd backend
set -a && source .env && set +a
uv run uvicorn app.main:app --host "$HOST" --port "$PORT"

# 前端（端口读 VITE_PORT，默认 8003；监听地址读 VITE_HOST，默认 0.0.0.0=允许局域网；
#       接口默认走同源 /api 代理到 127.0.0.1:$VITE_API_PORT，也支持 VITE_API_BASE 直连）
cd frontend
pnpm dev --strictPort
```

打开 `http://127.0.0.1:8003`。

## 远程 / 局域网访问

默认情况下（不加 `--local`）脚本会让前端监听 `0.0.0.0`，启动横幅里会打印一行共享地址：

```
共享访问   http://10.10.169.5:8003/   ← 发给同事，同一网络即可打开
```

把该地址发给同事，**同一局域网内**用浏览器直接打开即可，无需任何额外配置。

原理与注意点：

- **后端只监听 `127.0.0.1`**，不对外暴露。前端所有接口请求走同源相对路径 `/api/...`，由 Vite dev server 反向代理到 `http://127.0.0.1:$VITE_API_PORT`（见 `frontend/vite.config.ts`）。因此同事的浏览器只访问你机器的前端端口，`127.0.0.1` 不会被错误地解析成他们自己的电脑；SSE 解析进度流也经过该代理（代理已设 `accept-encoding: identity`，不会被缓冲）。
- **不要把 `VITE_API_BASE` 设成 `http://127.0.0.1:8002`**（`frontend/.env` 中已默认留空）。绝对回环地址在同事的浏览器里指向他们自己的机器，会直接请求失败。
- **macOS 会弹窗询问是否允许 `node` 接受传入连接**，需要点「允许」；若曾误点拒绝，可在「系统设置 → 网络 → 防火墙 → 选项」中放行。前后端端口不一致时，只需放行前端端口（默认 8003）。
- 不知道自己的 IP 时：`ipconfig getifaddr en0`（脚本已自动探测并打印）。
- 只允许本机访问：`./start.sh --local`。
- 如果同事不在同一局域网（跨公网），可选：SSH 隧道 `ssh -L 8003:127.0.0.1:8003 你的用户名@你的IP`；或使用 tailscale / cloudflared 之类的内网穿透工具。**注意：这类方式会把界面暴露到公网，请自行评估数据安全，建议只临时开启。**

## 测试与回归

```bash
cd backend
uv run pytest                                  # 全部测试
uv run python scripts/eval_replay.py --corpus tests/fixtures/eval --verbose   # 真实网关回归评估
```
