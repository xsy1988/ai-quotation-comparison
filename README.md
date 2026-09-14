# 采购报价对比Agent

上传多家供应商报价单（Excel / Word / 文字版 PDF / 扫描件图片），LLM 版面理解 + 脚本交叉验证解析，原子工艺映射后机械对比 + 顶部 AI 分析模块（表格化对比：含税单价/优势/劣势/风险/建议，支持子维度展开），支持就地修正、别名回流、新工艺决策与主数据自管理。前端为左侧菜单栏导航（发起报价对比 / 报价对比历史 / 报价单数据 / 供应商管理 / 基础数据维护）。

## 端口

| 服务 | 地址 | 登记 |
|---|---|---|
| 后端 API（FastAPI/uvicorn） | `127.0.0.1:8002` | port-manager：项目「采购报价对比Agent」service `api` |
| 前端开发服务器（Vite） | `127.0.0.1:8003` | port-manager：项目「采购报价对比Agent」service `frontend` |

端口已在项目 demo 端口管理器中登记（`docs/PORT_ALLOCATION_GUIDE.md`）。如需调整，先运行 `pm allocate` 申请新端口，再改 `backend/.env`（PORT/HOST）与 `frontend/.env`（VITE_PORT/VITE_API_BASE）。

## 启动

前置：LLM 网关可达（默认 `http://localhost:18080/v1`，可用环境变量 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` 覆盖）。

```bash
# 后端（端口/绑定读 backend/.env）
cd backend
set -a && source .env && set +a
uv run uvicorn app.main:app --host "$HOST" --port "$PORT"

# 前端（端口读 frontend/.env）
cd frontend
pnpm dev
```

打开 `http://127.0.0.1:8003`。

## 测试与回归

```bash
cd backend
uv run pytest                                  # 全部测试
uv run python scripts/eval_replay.py --corpus tests/fixtures/eval --verbose   # 真实网关回归评估
```
