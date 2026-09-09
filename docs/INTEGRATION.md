# LLM Gateway 接入指南

本文档面向需要调用 LLM 的 demo 项目，说明如何快速接入统一的 `LLM_Gateway` 服务。

---

## 1. 前提

`LLM_Gateway` 必须**先于消费项目启动**，默认监听：

```text
http://localhost:18080/v1
```

### 1.1 用 CLI 启动（推荐）

```bash
cd /Users/hg/工作/项目demo/LLM_Gateway
source .venv/bin/activate

llm-gateway start        # 默认 18080，带 --reload
llm-gateway status       # 检查健康状态
llm-gateway config       # 查看 provider 配置摘要
```

常用选项：

```bash
llm-gateway start --no-reload           # 关闭自动重载
llm-gateway start --port 18080          # 指定端口
llm-gateway status --port 18080         # 检查指定端口
```

### 1.2 手动启动

```bash
source .venv/bin/activate
export GATEWAY_API_KEY=local-demo-key
uvicorn llm_gateway.main:app --host 0.0.0.0 --port 18080 --reload
```

验证：

```bash
llm-gateway status
# 或
curl -H "Authorization: Bearer local-demo-key" http://localhost:18080/health
```

---

## 2. 统一接口

LLM_Gateway 提供 **OpenAI-compatible** 接口，消费端不需要写任何特殊逻辑。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查，返回 provider 列表 |
| `GET` | `/v1/models` | 列出当前可用的模型 |
| `POST` | `/v1/chat/completions` | 聊天补全（支持流式、vision、tools、JSON mode） |
| `POST` | `/v1/embeddings` | 文本向量化 |

### 2.1 认证

所有接口都需要在请求头里带网关 key：

```http
Authorization: Bearer local-demo-key
```

> 网关 key 由 `GATEWAY_API_KEY` 环境变量控制；真实上游 key 由网关集中管理，消费项目不需要关心。

### 2.2 标准调用示例

```bash
export GATEWAY_API_KEY=local-demo-key

# 聊天
curl -X POST http://localhost:18080/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.8-max",
    "messages": [{"role": "user", "content": "hello"}]
  }'

# 流式
curl -X POST http://localhost:18080/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.8-max",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true
  }'

# 向量化
curl -X POST http://localhost:18080/v1/embeddings \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "bge_m3_embed",
    "input": "测试文本"
  }'
```

---

## 3. 路由规则

### 3.1 按模型名自动路由

请求体里的 `model` 字段决定走哪个上游。例如：

- `"model": "qwen3.8-max"` → 阿里云 MaaS
- `"model": "kimi-for-coding"` → Kimi Cloud
- `"model": "bge_m3_embed"` → Kimi Embedding

当前可用模型可通过 `/v1/models` 查看。

### 3.2 强制指定上游

如果希望绕过自动路由，使用请求头：

```http
X-Gateway-Provider: maas-qwen38-max
```

provider_id 可通过 `/health` 查看。

### 3.3 禁用兜底切换

默认情况下，如果某个上游失败，网关会尝试切换到同模型的 fallback provider。如需禁用：

```http
X-Gateway-Fallback: false
```

### 3.4 透传字段

以下字段完整透传给上游，消费端按原生 OpenAI 方式使用即可：

- `messages`（含 `image_url` vision 内容）
- `stream`、`temperature`、`max_tokens`、`top_p`
- `tools` / `tool_choice`
- `response_format`（JSON mode）
- `extra_body`（例如 Qwen 的 `chat_template_kwargs.enable_thinking`）

---

## 4. 各项目接入示例

### 4.1 采购部成本对比

`model_config.json`：

```json
{
  "base_url": "http://localhost:18080/v1",
  "model": "qwen3.8-max",
  "vision_model": "qwen3.5-ocr",
  "ocr_model": "qwen3.5-ocr",
  "api_key_env": "GATEWAY_API_KEY",
  "temperature": 0.0,
  "timeout": 1200,
  "max_tokens": 16000,
  "max_retries": 2
}
```

运行前：

```bash
export GATEWAY_API_KEY=local-demo-key
```

### 4.2 差旅核销

`.env`：

```env
LLM_PROVIDER=llm_gateway
LLM_BASE_URL=http://localhost:18080/v1
LLM_API_KEY=local-demo-key
LLM_MODEL=qwen3.8-max
```

如果数据库已经 seed 过，需要重新执行 seed 或手动更新 `llm_configs` 表中的 `base_url` 和 `api_key`。

### 4.3 法律合规部信息爬取

`config.py` 默认值已指向网关：

```python
KIMI_BASE_URL = os.environ.get("KIMI_BASE_URL", "http://localhost:18080/v1")
KIMI_API_KEY = os.environ.get("KIMI_API_KEY", "local-demo-key")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "qwen3.8-max")
```

`llm_processor.py` 使用官方 `openai` SDK，无需改动。

### 4.4 Agent平台

Agent平台 自带 provider 管理，推荐在 UI / DB 中新增一条 provider：

| 字段 | 值 |
|---|---|
| `impl` | `openai_compatible` |
| `base_url` | `http://localhost:18080/v1` |
| `api_key` | `local-demo-key` |
| `model_name` | `qwen3.8-max`（或 `kimi-for-coding`、`deepseek-v4-flash` 等） |

`get_chat_model()` 与 `get_embeddings()` 无需改动。

---

## 5. Python SDK（可选）

LLM_Gateway 提供了便捷客户端：

```python
from llm_gateway.client import get_client, get_async_client

# 同步
client = get_client()
resp = client.chat.completions.create(
    model="qwen3.8-max",
    messages=[{"role": "user", "content": "hello"}],
)

# 异步
async_client = get_async_client()
resp = await async_client.chat.completions.create(
    model="qwen3.8-max",
    messages=[{"role": "user", "content": "hello"}],
)
```

也可以直接使用任意 OpenAI SDK：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:18080/v1",
    api_key="local-demo-key",
)
```

或者 LangChain：

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://localhost:18080/v1",
    api_key="local-demo-key",
    model="qwen3.8-max",
)
```

---

## 6. 环境变量

消费项目建议统一使用以下环境变量：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `GATEWAY_API_KEY` | 调用网关的 key | `local-demo-key` |
| `LLM_GATEWAY_BASE_URL` | 网关地址 | `http://localhost:18080/v1` |

---

## 7. 常见问题

### Q1: 网关启动时报端口被占用？

`8080` 已被其他 demo 服务占用，当前统一使用 `18080`。如果 `18080` 也被占用，可以修改 `providers.yaml` 里的 `port`，并在消费端同步改 `base_url`。

### Q2: 调用 `Qwen3.8-27B-FP8` 失败？

`qwen-local` 指向内网地址 `http://10.10.129.227:8867/v1`，在当前机器可能无法访问。到同一内网环境或启动本地 vLLM 后即可使用。

### Q3: 想新增一个模型或上游？

只需修改 `LLM_Gateway/providers.yaml`，添加/修改 provider 并重启网关。消费项目不需要任何改动。

### Q4: 网关返回 401？

检查请求头是否携带了正确的 `Authorization: Bearer local-demo-key`，以及网关的 `ENFORCE_AUTH` 是否为 `true`。

### Q5: 想临时强制某个上游？

加请求头 `X-Gateway-Provider: <provider_id>`，例如：

```bash
curl -X POST http://localhost:18080/v1/chat/completions \
  -H "Authorization: Bearer local-demo-key" \
  -H "X-Gateway-Provider: maas-deepseek-v4-flash" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
```

---

## 8. 文件索引

- `LLM_Gateway/providers.yaml` — 网关配置（真实上游 key，已 gitignore）
- `LLM_Gateway/providers.example.yaml` — 配置模板
- `LLM_Gateway/llm_gateway/client.py` — Python SDK
- `LLM_Gateway/tests/` — 单元测试
