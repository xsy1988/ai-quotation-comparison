"""prompts 注册表：模板加载与版本号、build_messages 填充与缺变量报错、
一致性 lint（业务代码不得残留 prompt 文本 / 模板提到的 schema 字段必须真实存在）、
constitution 拼进每个任务 prompt。"""

import json
import re
from pathlib import Path

import pytest

from app import prompts

BACKEND_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_DIR / "app"
DOCS_DIR = BACKEND_DIR.parent / "docs"

SEMVER_RE = re.compile(r"^v?\d+\.\d+\.\d+$")

ALL_TEMPLATES = [
    "constitution",
    "parse_quote",
    "match_atoms",
    "ocr_transcribe",
    "ai_analysis",
    "retry_feedback",
    "verify_calc",
]

TASKS = ["parse_quote", "match_atoms", "ocr_transcribe", "ai_analysis"]


def test_all_templates_loadable_and_semver_version():
    for name in ALL_TEMPLATES:
        body = prompts.get_prompt(name)
        assert body.strip(), name
        version = prompts.get_prompt_version(name)
        assert SEMVER_RE.fullmatch(version), f"{name} 版本号不符合 semver: {version!r}"


def test_fewshot_negative_loadable():
    text = (APP_DIR / "prompts" / "fewshots" / "parse_quote_negative_chuangfeng.md").read_text(
        encoding="utf-8"
    )
    assert "创锋" in text and "2.01" in text


def test_parse_quote_forbids_sensitive_info_in_other_info():
    """other_info 的排除清单：个人身份/签章信息 + 银行开户等收款信息，且不得以变形形式保留。"""
    text = prompts.get_prompt("parse_quote")
    assert "严格禁止写入" in text
    assert "银行账号" in text and "开户" in text
    for token in ("手机号", "姓名", "邮箱", "印章"):
        assert token in text
    assert "不得以任何变形" in text


def test_prompt_version_constant():
    assert SEMVER_RE.fullmatch(prompts.PROMPT_VERSION)


def test_unknown_template_raises():
    with pytest.raises(KeyError):
        prompts.get_prompt("no_such_task")


def test_build_messages_parse_quote_fill():
    messages = prompts.build_messages(
        "parse_quote", {"categories": "CAT-WJWK 五金外壳", "ir_serialized": "报价单|1|1:铝材"}
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    system, user = messages
    assert "你是采购报价单结构化解析助手" in system["content"]
    # 模板变量已填充
    assert "CAT-WJWK 五金外壳" in user["content"]
    assert "报价单|1|1:铝材" in user["content"]
    # fewshot 负例自动注入
    assert "创锋" in user["content"]
    # schema brief 仍是模板的一部分
    for token in ('"offers"', "match_path", "sga_tax", "packaging_transport", "_self_check"):
        assert token in user["content"]


def test_build_messages_missing_var_raises():
    with pytest.raises(KeyError):
        prompts.build_messages("match_atoms", {"category": "CAT-WJWK"})


def test_build_retry_messages():
    for kind, marker in (
        ("json_unparseable", "上次输出无法解析"),
        ("schema_invalid", "quote_schema v1.1"),
        # traceability 反馈必须给出可执行修法（历史事故：笼统的「找不到的填 null」不可执行）
        ("traceability", "amount_not_in_ir"),
    ):
        messages = prompts.build_retry_messages(kind, ["$.offers[0].basic.currency: 枚举校验失败"])
        assert [m["role"] for m in messages] == ["assistant", "user"]
        assert "枚举校验失败" in messages[1]["content"]
        assert marker in messages[1]["content"]
    with pytest.raises(KeyError):
        prompts.build_retry_messages("no_such_kind", [])


def test_constitution_in_every_task_prompt():
    constitution = prompts.get_prompt("constitution")
    for task in TASKS:
        context = {
            "parse_quote": {"categories": "c", "ir_serialized": "s|1|1:v"},
            "match_atoms": {"category": "c", "candidates": "x", "items": "y"},
            "ocr_transcribe": {},
            "ai_analysis": {"comparison": "{}"},
        }[task]
        user = prompts.build_messages(task, context)[-1]["content"]
        # constitution 全文（逐条宪法）拼在任务模板之前
        assert user.startswith(constitution)
        for article in ("数字溯源", "缺失即 null", "共享单元格", "只抽取不计算", "自检报告"):
            assert article in user, f"{task} 缺宪法条款: {article}"


def test_no_prompt_text_left_in_business_code():
    """一致性 lint：backend/app 下（prompts 包自身除外）不得出现 prompt 文本。"""
    offenders = []
    for py in APP_DIR.rglob("*.py"):
        if "prompts" in py.parts or "__pycache__" in py.parts:
            continue
        if "你是" in py.read_text(encoding="utf-8"):
            offenders.append(py.relative_to(BACKEND_DIR))
    assert not offenders, f"业务代码中残留 prompt 文本（你是…）: {offenders}"


def _schema_roots() -> dict:
    schema = json.loads((DOCS_DIR / "quote_schema.json").read_text(encoding="utf-8"))
    roots = dict(schema.get("properties") or {})
    roots.update(schema.get("definitions") or {})
    # _self_check 是 schema 之外的附加契约（parse_quote 输出要求），其字段按任务约定校验
    roots["_self_check"] = {
        "properties": {
            "amounts_traceable": {},
            "no_invented_values": {},
            "tax_amount_null_if_unprinted": {},
            "null_fields": {},
            "uncertain_cells": {},
        }
    }
    return roots


def _resolve_path(node: dict, segments: list[str]) -> bool:
    for seg in segments:
        props = node.get("properties") or {}
        if seg in props:
            node = props[seg]
            continue
        # 数组节点：字段属于 items 的元素 schema（如 unit_price.sga_tax.items[].amount_per_pc）
        if node.get("type") == "array" and isinstance(node.get("items"), dict):
            item_props = node["items"].get("properties") or {}
            if seg in item_props:
                node = item_props[seg]
                continue
        return False
    return True


def test_template_schema_fields_exist_in_quote_schema():
    """一致性 lint：prompts 模板里提到的 schema 字段路径必须是 quote_schema.json 真实存在的字段。"""
    roots = _schema_roots()
    dotted = re.compile(r"\b([a-z][a-z0-9_]*(?:\[\d+\])?(?:\.[a-z][a-z0-9_]*(?:\[\d+\])?)+)")
    md_files = sorted((APP_DIR / "prompts").rglob("*.md"))
    assert len(md_files) >= 7
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        for m in dotted.finditer(text):
            token = m.group(1)
            segments = [s for s in re.split(r"\.|\[\d+\]", token) if s]
            if segments[0] not in roots:
                continue  # 非 schema 根的普通点号文本（如 quote_schema.json），不做字段断言
            assert _resolve_path(roots[segments[0]], segments[1:]), (
                f"{md.name} 提到不存在的 schema 字段路径: {token}"
            )
    # spec 点名字段：unit_price / sga_tax 必须真实存在于 schema 且被 parse_quote 模板提及
    assert "unit_price" in roots and "sga_tax" in roots["unit_price"]["properties"]
    parse_quote = prompts.get_prompt("parse_quote")
    for field in ("unit_price", "sga_tax"):
        assert field in parse_quote


def test_ai_analysis_prompt_explains_null_semantics():
    """null 纪律：null 模块合计不是 0，须在劣势/风险里点明「未印出，合计只是下限」。"""
    text = prompts.get_prompt("ai_analysis")
    assert "null 语义" in text
    assert "不是 0" in text and "只是下限" in text
    assert "calc_abnormal" in text


def test_ai_analysis_prompt_keeps_input_supplier_order():
    """顺序纪律：输出的 suppliers 顺序必须与输入一致（= 比价表格的供应商列顺序），
    否则「AI 分析」与「报价对比」两张表的列前后对不上。"""
    text = prompts.get_prompt("ai_analysis")
    assert "不要按价格重排" in text
