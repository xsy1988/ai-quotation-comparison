# 项目 Demo 端口申请与使用规范

> 适用范围：`/Users/hg/工作/项目demo/` 下的所有子项目及其新增服务。
>
> 目的：避免本地开发时出现端口抢占、默认端口冲突、文档与实际情况不一致的问题。

## 1. 为什么需要统一管理端口

在项目 demo 目录下，多个 Python/FastAPI/Node 服务同时开发。如果每个项目都随意挑选端口，很容易出现：

- 服务 A 默认用 `8080`，服务 B 也默认用 `8080`，同时启动时必有一个失败；
- 新成员按 README 启动项目，却不知道某个端口已被占用；
- 某个项目下线后，端口仍处于“无主”状态，没人敢复用。

因此，所有**新项目、新服务、新组件**在确定监听端口前，必须通过 `port-manager` 申请并登记。

## 2. 哪些情况需要申请端口

以下任一情况都需要先在 `port-manager` 登记：

- [ ] 新增一个会监听 TCP/UDP 端口的进程（HTTP API、WebSocket、gRPC、数据库、缓存、队列等）；
- [ ] 现有项目新增一个独立服务（例如从单体拆出 worker、scheduler、parser）；
- [ ] 现有服务的端口需要变更（如从 `8080` 改到 `18080`）；
- [ ] 引入 docker-compose 等容器服务，需要将容器端口映射到宿主机；
- [ ] 为本地测试临时暴露的服务（即使只监听 `127.0.0.1`）。

**不需要申请的情况**：

- 操作系统/桌面应用自动分配的随机端口；
- 纯内部 Unix Socket，不监听 TCP；
- 开发工具（如 VS Code 调试端口、Vite HMR 随机端口）临时使用，且重启后会变。

## 3. 申请端口的步骤

### 3.1 进入端口管理服务目录

```bash
cd /Users/hg/工作/项目demo/port-manager
source .venv/bin/activate
```

### 3.2 申请新端口

```bash
pm allocate --project <项目名> --service <服务名> \
  --component <组件说明> --desc <用途描述>
```

示例：

```bash
pm allocate --project 智能合同审查 --service api \
  --component "FastAPI backend" \
  --desc "合同审查 Agent 的 REST API，供前端调用"
```

输出示例：

```text
✅ 已分配端口：8001
   项目：智能合同审查
   服务：api
   监听：127.0.0.1:8001
```

### 3.3 如果已经有默认端口，想保留

如果你的项目历史原因已经用了某个端口（例如 `8080`），请显式登记：

```bash
pm register 8080 --project 法律合规部信息爬取 --service web-server \
  --component "scripts/serve_web.py" \
  --desc "爬虫结果静态文件 Web 服务器"
```

> ⚠️ 如果该端口已被其他项目登记，`pm register` 会失败，需要先协商或改端口。

### 3.4 将分配的端口写回项目配置

**禁止在代码里硬编码端口**（除非该端口已通过 `pm` 登记）。推荐做法：

1. 在项目根目录或 `backend/` 下创建/修改 `.env`：

```env
# .env
PORT=8001
HOST=127.0.0.1
```

2. 启动脚本读取环境变量：

```python
# main.py 或启动脚本
import os
import uvicorn

port = int(os.environ.get("PORT", "8000"))
host = os.environ.get("HOST", "127.0.0.1")
uvicorn.run(app, host=host, port=port)
```

3. 更新项目 README，写明：

```markdown
## 端口

本服务默认监听 `127.0.0.1:8001`，端口已在项目 demo 端口管理器中登记。
如需调整，请先运行 `pm allocate` 申请新端口，再修改 `.env`。
```

## 4. 端口范围规范

| 范围 | 用途 | 说明 |
|------|------|------|
| `8000-8999` | 项目自定义服务 | 默认自动分配区间 |
| `5000-5999` | 通用框架/解析服务 | 如 Flask、Docling parser、Vite 等 |
| `18000-18999` | 网关/代理类服务 | 如 LLM_Gateway 使用 `18080` |
| `19000-19099` | 工具/管理服务 | 如 port-manager 自身 API 预留 `19090` |
| `5432` | PostgreSQL | 数据库默认端口，已被 Agent平台 占用 |
| `6379` | Redis | 如未来引入，需登记 |
| `3306` | MySQL | 系统保留，不建议本地使用 |

> `pm allocate` 默认只会在 `8000-8999` 内分配，并自动跳过系统保留端口和已登记端口。

## 5. 命名规范

登记时请使用清晰、统一的项目/服务名，便于后续检索：

- **project**：使用项目目录名或简称，如 `Agent平台`、`LLM_Gateway`、`法律合规部信息爬取`。
- **service**：使用技术角色，如 `backend`、`frontend`、`gateway`、`parser`、`worker`、`scheduler`、`db`。
- **component**：进一步说明，如 `FastAPI uvicorn`、`Vite dev server`、`docling-serve-cpu`、`PostgreSQL pgvector`。
- **description**：一句话说明用途和消费方，如 "供前端 Vite 反向代理到 /api"。

## 6. 监听地址规范

| 场景 | 推荐绑定 | 说明 |
|------|----------|------|
| 纯本地开发服务 | `127.0.0.1` | 最安全，仅本机可访问 |
| 需要局域网/容器访问 | `0.0.0.0` | 需评估安全风险，登记时注明 |
| 数据库、缓存 | `127.0.0.1` | 默认不对外暴露 |
| docker-compose 映射 | `127.0.0.1:<port>:<container_port>` | 尽量只映射到本地回环 |

> ⚠️ 绑定 `0.0.0.0` 的服务在 README 中必须明确说明，并建议使用认证/防火墙。

## 7. 释放与下线

项目下线、服务合并或端口变更时，必须释放旧端口：

```bash
pm release 8001
```

释放后该端口会回到可用池，其他项目可申请。

## 8. 定期检查

建议每周或每次新增服务前执行一次扫描：

```bash
pm scan --dry-run
```

- 如果发现有未登记的项目服务，请及时 `pm register` 或 `pm allocate` 补登记；
- 如果发现有已下线但仍占用端口的服务，请 `pm release`。

## 9. 示例：新增一个项目

假设要新增项目 `智能合同审查`，包含 `backend` 和 `frontend`：

```bash
# 1. 申请后端端口
cd /Users/hg/工作/项目demo/port-manager
source .venv/bin/activate
pm allocate --project 智能合同审查 --service backend \
  --component "FastAPI" --desc "合同审查 REST API"
# 假设分配到 8002

# 2. 申请前端端口
pm allocate --project 智能合同审查 --service frontend \
  --component "Vite" --desc "React 前端开发服务器"
# 假设分配到 8003

# 3. 在项目里配置 .env
cd /Users/hg/工作/项目demo/智能合同审查
cat > backend/.env <<EOF
PORT=8002
HOST=127.0.0.1
EOF

cat > frontend/.env <<EOF
VITE_PORT=8003
EOF

# 4. 启动脚本读取环境变量
# backend/main.py: port = int(os.environ.get("PORT", "8000"))
# frontend/vite.config.ts: server.port = parseInt(process.env.VITE_PORT || "5173")

# 5. 更新项目 README 的端口说明
```

## 10. 冲突处理

如果 `pm allocate` 提示某端口已被占用：

1. 查看占用者：`pm check <port>`
2. 如果是历史遗留，可协商释放：`pm release <port>`（需确认该服务已下线）；
3. 如果无法释放，为你的服务申请新端口，并修改项目 `.env`；
4. 严禁通过 `pm register --force` 强行覆盖他人登记，除非得到项目所有者同意。

## 11. 参考命令速查

```bash
pm list                              # 列出所有登记
pm list --project Agent平台           # 按项目过滤
pm check 8080                        # 检查端口
pm allocate --project X --service Y  # 自动分配
pm register 8020 --project X --service Y   # 登记指定端口
pm release 8020                      # 释放端口
pm scan --dry-run                    # 扫描差异（不写入）
pm scan --import                     # 扫描并导入未登记端口
pm serve --port 19090                # 启动 HTTP API
```

## 12. 相关文件

- 注册表：`/Users/hg/工作/项目demo/port-manager/registry.json`
- 服务代码：`/Users/hg/工作/项目demo/port-manager/port_manager/`
- 总览文档：`/Users/hg/工作/项目demo/port-manager/README.md`
