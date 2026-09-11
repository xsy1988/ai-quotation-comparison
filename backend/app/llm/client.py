"""LLM 网关客户端：OpenAI SDK 兼容，指向本地网关。

配置统一走 app.config.settings（默认值见 config.py）：
  settings.llm_base_url / llm_api_key / llm_timeout / llm_model / llm_enable_thinking
温度固定 0（结构化解析任务，要求确定性输出）。

线程安全：chat_json 默认每次调用新建 OpenAI 客户端（build_client），无跨线程共享
session/连接池，文件级并发解析（pipeline ThreadPoolExecutor）下可直接多线程调用。

chat_json 返回 (解析后的 JSON dict, usage 元信息)。两类失败：
  LLMUnavailable —— 网关不可达/超时/5xx，调用方应降级
  LLMError       —— 其他失败（含输出不是合法 JSON）
"""

import json
import re
import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from app.config import settings


class LLMError(Exception):
    """LLM 调用失败（输出非法、参数错误等）。"""


class LLMUnavailable(LLMError):
    """LLM 网关不可达/超时/5xx，调用方应降级到规则逻辑。"""


def build_client() -> OpenAI:
    return OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
    )


def chat_json(
    messages: list[dict],
    *,
    model: str | None = None,
    client: OpenAI | None = None,
) -> tuple[dict | list, dict]:
    """chat.completions（json_object 模式）→ (解析后的 JSON 对象/数组, usage)。

    usage 含 model/prompt_tokens/completion_tokens/total_tokens/elapsed_ms。
    """
    llm = client or build_client()
    model = model or settings.llm_model
    # 网关为推理模型，默认关思考（结构化解析任务，关思考后延迟从分钟级降到秒级且输出更稳定）；
    # 需要 reasoning 时设 LLM_ENABLE_THINKING=1
    extra_body = None
    if not settings.llm_enable_thinking:
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
    # 部分模型（如专用 OCR）无视"不要 markdown"的指令，输出 ```json 包裹；剥离围栏后再解析
    if content.startswith("```"):
        fence = re.search(r"^```(?:json)?\s*(.*?)\s*```$", content, re.DOTALL)
        if fence:
            content = fence.group(1).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"LLM 输出不是合法 JSON：{e}；原文前 200 字：{content[:200]}") from e
    if not isinstance(parsed, (dict, list)):
        raise LLMError(f"LLM 输出不是 JSON 对象或数组：{content[:200]}")

    u = resp.usage
    usage = {
        "model": model,
        "prompt_tokens": getattr(u, "prompt_tokens", None) if u else None,
        "completion_tokens": getattr(u, "completion_tokens", None) if u else None,
        "total_tokens": getattr(u, "total_tokens", None) if u else None,
        "elapsed_ms": elapsed_ms,
    }
    return parsed, usage
