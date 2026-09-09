"""LLM 网关客户端：OpenAI SDK 兼容，指向本地网关。

配置全部走环境变量（带默认值）：
  LLM_BASE_URL  默认 http://localhost:18080/v1
  LLM_API_KEY   默认 local-demo-key
  LLM_MODEL     默认 qwen3.8-max
温度固定 0（结构化解析任务，要求确定性输出）。

chat_json 返回 (解析后的 JSON dict, usage 元信息)。两类失败：
  LLMUnavailable —— 网关不可达/超时/5xx，调用方应降级
  LLMError       —— 其他失败（含输出不是合法 JSON）
"""

import json
import os
import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI


class LLMError(Exception):
    """LLM 调用失败（输出非法、参数错误等）。"""


class LLMUnavailable(LLMError):
    """LLM 网关不可达/超时/5xx，调用方应降级到规则逻辑。"""


def build_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "http://localhost:18080/v1"),
        api_key=os.environ.get("LLM_API_KEY", "local-demo-key"),
        timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
    )


def chat_json(
    messages: list[dict],
    *,
    model: str | None = None,
    client: OpenAI | None = None,
) -> tuple[dict, dict]:
    """chat.completions（json_object 模式）→ (JSON dict, usage)。

    usage 含 model/prompt_tokens/completion_tokens/total_tokens/elapsed_ms。
    """
    llm = client or build_client()
    model = model or os.environ.get("LLM_MODEL", "qwen3.8-max")
    # 网关为推理模型，默认关思考（结构化解析任务，关思考后延迟从分钟级降到秒级且输出更稳定）；
    # 需要 reasoning 时设 LLM_ENABLE_THINKING=1
    extra_body = None
    if os.environ.get("LLM_ENABLE_THINKING", "0") != "1":
        extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
    started = time.monotonic()
    try:
        resp = llm.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
            extra_body=extra_body,
        )
    except (APIConnectionError, APITimeoutError) as e:
        raise LLMUnavailable(f"LLM 网关不可达或超时：{e}") from e
    except APIStatusError as e:
        if e.status_code >= 500:
            raise LLMUnavailable(f"LLM 网关 {e.status_code}：{e.message}") from e
        raise LLMError(f"LLM 调用失败（{e.status_code}）：{e.message}") from e
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)

    content = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 输出不是合法 JSON：{e}；原文前 200 字：{content[:200]}") from e
    if not isinstance(parsed, dict):
        raise LLMError(f"LLM 输出不是 JSON 对象：{content[:200]}")

    u = resp.usage
    usage = {
        "model": model,
        "prompt_tokens": getattr(u, "prompt_tokens", None) if u else None,
        "completion_tokens": getattr(u, "completion_tokens", None) if u else None,
        "total_tokens": getattr(u, "total_tokens", None) if u else None,
        "elapsed_ms": elapsed_ms,
    }
    return parsed, usage
