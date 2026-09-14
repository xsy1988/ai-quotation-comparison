# 采购报价对比Agent

上传多家供应商报价单（Excel / Word / 文字版 PDF / 扫描件图片），LLM 版面理解 + 脚本交叉验证解析，原子工艺映射后机械对比 + 顶部 AI 分析模块（表格化对比：含税单价/优势/劣势/风险/建议，支持子维度展开），支持就地修正、别名回流、新工艺决策与主数据自管理。前端为左侧菜单栏导航（发起报价对比 / 报价对比历史 / 报价单数据 / 供应商管理 / 基础数据维护）。

## 端口

| 服务 | 地址 | 登记 |
|---|---|---|
| 后端 API（FastAPI/uvicorn） | `127.0.0.1:8002` | port-manager：项目「采购报价对比Agent」service `api` |
| 前端开发服务器（Vite） | `127.0.0.1:8003` | port-manager：项目「采购报价对比Agent」service `frontend` |

端口已在项目 demo 端口管理器中登记（`docs/PORT_ALLOCATION_GUIDE.md`）。如需调整，先运行 `pm allocate` 申请新端口，再改 `backend/.env`（PORT/HOST）与 `frontend/.env`（VITE_PORT/VITE_API_BASE）。

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

# 前端（端口读 VITE_PORT，默认 8003；后端地址读 VITE_API_BASE）
cd frontend
pnpm dev --strictPort
```

打开 `http://127.0.0.1:8003`。

## 测试与回归

```bash
cd backend
uv run pytest                                  # 全部测试
uv run python scripts/eval_replay.py --corpus tests/fixtures/eval --verbose   # 真实网关回归评估
```
