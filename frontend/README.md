# 采购报价对比 Agent — 前端

React SPA，三页面：发起对比（`/`）、解析进度（`/tasks/:id/progress`）、比价界面（`/tasks/:id/comparison`）。

## 技术栈

Vite + React 18 + TypeScript + pnpm + Ant Design 5 + TanStack Query + React Router + dayjs + axios

## 端口（port-manager 分配）

| 服务 | 地址 |
|---|---|
| 前端 Vite dev server | http://127.0.0.1:8003（写死在 `vite.config.ts` 的 `server.port`） |
| 后端 FastAPI | http://127.0.0.1:8002（可用 `frontend/.env` 的 `VITE_API_BASE` 覆盖，模板见 `.env.example`） |

## 启动

```bash
pnpm install
pnpm dev        # http://127.0.0.1:8003
pnpm build      # tsc -b && vite build → dist/
```

## 目录

```
src/
  api/client.ts              # axios 封装 + SSE 订阅（subscribeProgress 返回取消函数）
  types/index.ts             # 与 compare_engine.get_comparison 输出对齐的 TS 类型
  pages/
    CreatePage.tsx           # 发起页：Upload.Dragger 多 .xlsx + 项目名 + 历史任务 Modal
    ProgressPage.tsx         # 进度页：EventSource 接 SSE，五段 Steps，断线重连一次
    ComparisonPage.tsx       # 比价界面：徽标行 + 四个对比区域
  components/
    HierarchyTable.tsx       # 层级金额对比表（核心），加工费行可展开明细
    DrawerTabs.tsx           # 加工费专区：工艺域/工艺阶段/工艺类别三维度抽屉
    FingerprintTable.tsx     # 指纹对齐：打包口径对齐展示
    ToolingSection.tsx       # 模治具：模具/治具/钢网
    Amount.tsx               # 统一金额渲染（两位小数；null → “路线未含此工序”）
    compareUtils.ts          # 徽标维度 → 供应商聚合等纯函数
```

## 空值语义

后端 `get_comparison` 中供应商未报某行/抽屉时值为 `null`，前端渲染灰色小字"路线未含此工序"，绝不显示 0 或空白。
