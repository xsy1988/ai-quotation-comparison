# 采购报价对比Agent —— 后端

FastAPI + SQLite。主数据与业务数据落 SQLite，原始文件与 JSON 快照走文件系统归档。
设计文档见仓库根目录 `docs/`。

## 端口

本服务监听 `127.0.0.1:8002`，端口已在项目 demo 端口管理器中登记（项目：采购报价对比Agent，服务：api）。
如需调整：先到 `../port-manager` 执行 `pm allocate` 申请新端口，再更新 `.env`。

## 环境准备

```bash
cd backend
uv sync                 # 安装依赖（python >= 3.12，勿用系统 python3）
cp .env.example .env    # 可选，默认 PORT=8000
```

## 初始化数据库

```bash
uv run python -c "from app.db import init_db; init_db()"
```

默认库文件 `backend/data/quotes.db`，可用环境变量 `QUOTES_DB_PATH` 覆盖。

## 导入主数据

```bash
uv run python scripts/import_master_data.py
```

数据源为仓库根的 `docs/原子工艺清单v2.xlsx`。幂等：重跑会先清主数据表再导入（不动业务表与 supplier）。

## 接入与落库（流水线骨架）

```bash
# 查重 → Excel→IR → 归档（第二次跑同文件命中查重复用历史 IR；--force 强制重跑）
uv run python scripts/run_ingest.py <报价单.xlsx>

# 继续演示落库：quote_schema JSON → 校验 → 快照 → 拍平写 quote/quote_line/tooling_line
uv run python scripts/run_ingest.py <报价单.xlsx> --demo-persist tests/fixtures/sample_quote.json
```

注意：persist 当前每次新建 comparison_task，同一文件重复 persist 会重复入库；幂等由后续的任务编排（pipeline）统一解决。

## 映射（L1 别名匹配 + 费用类型推断）

```bash
uv run python scripts/run_mapping.py <quote_id>
```

对一份已落库的报价单：加工费条目跑 L1 精确匹配（词库 = 原子名 ∪ 别名，归一化后匹配；
唯一命中→high/L1_alias 且别名 hit_count++；歧义/未命中→进 unmatched_term 词池）；
包装运输/损管利税条目推断 item_type。结果回写 quote_line 与 JSON 快照，并重算 quote.flags。

## 启动服务

```bash
uv run python app/main.py        # 或 uv run uvicorn app.main:app --host 127.0.0.1 --port 8002
```

健康检查：`curl http://127.0.0.1:8002/health` → `{"status":"ok"}`。

## 文件存储（对象存储）

报价单上传后先落对象存储（原件二进制），再进解析流水线；解析结果与报价单详情都带源文件名称，前端可在线预览（PDF/图片）或下载。

- 默认本地实现（内容寻址）：`app/storage.py`，key = `objects/<sha256 前两位>/<sha256><扩展名>`，同一文件重复上传走硬链接，只占一份空间。
- 元数据落在 `stored_object` 表（每次上传一条：原名/大小/内容类型/SHA256/存储 key/时间）。
- 存储失败不阻断解析，只在 `parse_log` 打 `store_failed` 告警。
- 配置：`OBJECT_STORE_BACKEND`（`local` 默认 / `s3` 预留，需装 boto3 后补 `S3ObjectStore`）、`OBJECT_STORE_DIR`（默认 `backend/data/object_store`）。

## 测试

```bash
uv run pytest
```

## 目录结构

```
app/
  main.py        FastAPI 入口（/health，CORS 全开，本地 demo）
  db.py          SQLite 连接管理 + init_db()
  schema.sql     全部建表 DDL（主数据 8 表 + 业务 7 表）
  storage.py     对象存储（ObjectStore 抽象 + 本地内容寻址实现）
scripts/
  import_master_data.py   原子清单 xlsx → 主数据表
data/            SQLite 库文件 / 上传归档 / 快照 / 对象存储（*.db 已 gitignore）
tests/           pytest
```
