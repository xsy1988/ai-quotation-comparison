"""LLM-B 核算员（影子模式）：对 derive 修正后的单个 offer 做计算复核，只记录结论、不改动任何数值。

与 L2 勾稽校验（validators.validate_l2_reconcile，脚本侧 blame=B）互为参照：脚本按固定
规则判，LLM-B 自由复核，两者结论并排写 parse_log（stage="verify", action="shadow"），
积累"脚本 vs LLM-B"判例供后续转正评估。本期影子模式不生效——verify 结论不回写 offer、
不影响 calc_check / flags / 重试。

永不抛异常：LLMError/LLMUnavailable、输出结构不符、任何意外异常统一降级为
{"verdict": "skipped", "reason": ...}；幂等只读。
"""

import json
import sqlite3
from typing import Any

from app import prompts
from app.ir import IR
from app.llm import client as llm_client
from app.llm.client import LLMError
from app.pipeline.layout_understand import _serialize_ir

VALID_VERDICTS = ("pass", "issues")
"""LLM-B 合法结论取值；其余一律视为结构不符，降级 skipped。"""


def _list_of_dicts(parsed: dict, key: str) -> list[dict]:
    """宽容取 checks/suspects 数组：缺省 []；非数组抛 LLMError，非 dict 条目丢弃。"""
    value = parsed.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise LLMError(f"LLM-B 输出 {key} 不是数组：{type(value).__name__}")
    return [item for item in value if isinstance(item, dict)]


def _normalize(parsed: Any, usage: dict | None) -> dict:
    """结构校验与整理：verdict 必须合法，checks/suspects 宽容收集。"""
    if not isinstance(parsed, dict):
        raise LLMError(f"LLM-B 输出不是 JSON 对象：{type(parsed).__name__}")
    verdict = parsed.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise LLMError(f"LLM-B 输出 verdict 非法：{verdict!r}（应为 pass/issues）")
    return {
        "verdict": verdict,
        "checks": _list_of_dicts(parsed, "checks"),
        "suspects": _list_of_dicts(parsed, "suspects"),
        "usage": usage or None,
        "prompt_version": prompts.get_prompt_version("verify_calc"),
    }


def run_verify_shadow(
    offer: dict,
    ir,
    conn: sqlite3.Connection | None = None,
    chat_fn=None,
) -> dict | None:
    """影子模式核算复核（永不抛异常，只读不改 offer）。

    组装 verify_calc prompt（offer JSON 序列化 + IR 单元格序列化，序列化复用
    layout_understand._serialize_ir）→ chat_fn（默认 llm_client.chat_json，注入点便于
    测试）→ 校验 verdict/checks/suspects 结构。LLM 故障或结构不符返回
    {"verdict": "skipped", "reason": ...}；正常返回
    {"verdict", "checks", "suspects", "usage", "prompt_version"}。
    conn 为预留参数（后续加载参照数据用），本期影子模式不使用；ir 为 IR 对象或 dict。
    """
    del conn  # 预留参数，本期不使用
    fn = chat_fn or llm_client.chat_json
    try:
        if isinstance(ir, dict):
            ir = IR.from_dict(ir)
        ir_text = _serialize_ir(ir) if ir is not None else "（无 IR，仅按 offer 内部勾稽复核）"
        messages = prompts.build_messages(
            "verify_calc",
            {
                "offer_serialized": json.dumps(offer, ensure_ascii=False),
                "ir_serialized": ir_text,
            },
        )
        parsed, usage = fn(messages)
        return _normalize(parsed, usage)
    except LLMError as e:
        return {"verdict": "skipped", "reason": str(e)}
    except Exception as e:  # 兜底：影子模式绝不拖垮流水线
        return {"verdict": "skipped", "reason": f"unexpected {type(e).__name__}: {e}"}
