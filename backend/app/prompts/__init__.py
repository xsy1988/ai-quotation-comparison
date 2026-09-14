"""LLM prompt 集中注册表：模板文件加载、版本管理、统一消息组装。

所有 LLM 任务的 prompt 文本集中在本包维护（constitution.md 全局宪法 + 各任务模板 +
fewshots/ 负例 + retry_feedback.md 重试反馈模板），业务代码通过 get_prompt /
build_messages 取用，不在业务文件里保留大段 prompt 文本。

每个模板文件首行为版本注释：<!-- version: vX.Y.Z -->；
build_messages(task, context) 返回 [{"role": "system", ...}, {"role": "user", ...}]，
user 内容 = constitution.md + 任务模板（{var} 变量填充，缺失即抛 KeyError）。
"""

import re
from pathlib import Path
from typing import Any

PROMPT_VERSION = "v2.1.0"

_DIR = Path(__file__).parent
_FEWSHOT_DIR = _DIR / "fewshots"

_VERSION_RE = re.compile(r"<!--\s*version:\s*(v?\d+\.\d+\.\d+)\s*-->")
_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 任务名 → 模板文件
_PROMPT_FILES = {
    "constitution": "constitution.md",
    "parse_quote": "parse_quote.md",
    "match_atoms": "match_atoms.md",
    "ocr_transcribe": "ocr_transcribe.md",
    "ai_analysis": "ai_analysis.md",
    "retry_feedback": "retry_feedback.md",
    "verify_calc": "verify_calc.md",
}

# system 角色短句（统一在此维护；None 表示该任务无 system 消息）
_SYSTEM_PROMPTS: dict[str, str | None] = {
    "parse_quote": "你是采购报价单结构化解析助手，只输出 JSON。",
    "match_atoms": "你是工艺原子匹配助手，只输出 JSON。",
    "ocr_transcribe": None,
    "ai_analysis": (
        "你是一名资深采购比价顾问，擅长解读多家供应商的零件报价对比结果。"
        "你只依据输入的结构化对比数据作答，绝不臆造数字。"
        "所有引用的金额必须直接来自输入数据，禁止自行计算或编造。"
        "用中文输出，严格按给定 JSON schema 输出，不要输出 schema 之外的任何文字。"
    ),
    "verify_calc": "你是报价核算复核员，只核对数字逻辑，只输出 JSON。",
}

# 模板变量名 → fewshots/ 下的负例文件名（build_messages 自动注入，调用方无需传）
_FEWSHOT_VARS = {"fewshot_negative": "parse_quote_negative_chuangfeng.md"}

_cache: dict[str, tuple[str, str]] = {}
_fewshot_cache: dict[str, str] = {}


def _load(name: str) -> tuple[str, str]:
    """读模板文件，返回 (版本号, 正文)（剔除首行版本注释）；未知名字抛 KeyError。"""
    if name in _cache:
        return _cache[name]
    try:
        filename = _PROMPT_FILES[name]
    except KeyError:
        raise KeyError(f"未知 prompt 模板: {name}（可选：{sorted(_PROMPT_FILES)}）") from None
    text = (_DIR / filename).read_text(encoding="utf-8")
    m = _VERSION_RE.search(text.splitlines()[0] if text.splitlines() else "")
    if not m:
        raise ValueError(f"prompt 模板 {filename} 缺少版本注释 <!-- version: vX.Y.Z -->")
    body = _VERSION_RE.sub("", text, count=1).strip()
    _cache[name] = (m.group(1), body)
    return _cache[name]


def get_prompt(name: str) -> str:
    """返回模板正文（不含版本注释）。"""
    return _load(name)[1]


def get_prompt_version(name: str) -> str:
    """返回模板版本号（如 v2.0.0）。"""
    return _load(name)[0]


def _load_fewshot(filename: str) -> str:
    if filename not in _fewshot_cache:
        _fewshot_cache[filename] = (_FEWSHOT_DIR / filename).read_text(encoding="utf-8")
    return _fewshot_cache[filename]


def fill_template(template: str, context: dict[str, Any]) -> str:
    """填充 {var} 变量；模板里出现但 context 缺失的变量抛 KeyError。"""
    missing = sorted({m.group(1) for m in _VAR_RE.finditer(template)} - set(context))
    if missing:
        raise KeyError(f"prompt 模板缺少变量: {missing}")
    return _VAR_RE.sub(lambda m: str(context[m.group(1)]), template)


def build_messages(task: str, context: dict[str, Any] | None = None) -> list[dict]:
    """统一组装消息：constitution 全局宪法 + 任务模板（含 fewshot 负例自动注入）填充。

    返回 [{"role": "system", ...}, {"role": "user", ...}]；无 system 短句的任务只返回 user。
    """
    ctx = dict(context or {})
    for var, filename in _FEWSHOT_VARS.items():
        ctx.setdefault(var, _load_fewshot(filename))
    user = fill_template(get_prompt(task), ctx)
    constitution = get_prompt("constitution")
    user = f"{constitution}\n\n{user}"
    messages: list[dict] = []
    system = _SYSTEM_PROMPTS.get(task)
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    return messages


_RETRY_SECTION_RE = re.compile(r"^##\s+(\S+)\s*\n(.*?)(?=^##\s|\Z)", re.M | re.S)


def _retry_sections() -> dict[str, tuple[str, str]]:
    """解析 retry_feedback.md：kind → (assistant 占位, user 模板)。"""
    sections: dict[str, tuple[str, str]] = {}
    for m in _RETRY_SECTION_RE.finditer(get_prompt("retry_feedback")):
        kind, body = m.group(1), m.group(2)
        assistant = re.search(r"^assistant：(.+)$", body, re.M)
        user = re.search(r"```(?:text)?\n(.*?)```", body, re.S)
        if not assistant or not user:
            raise ValueError(f"retry_feedback.md 的 {kind} 段格式不完整（需要 assistant：行与 ``` 代码块）")
        sections[kind] = (assistant.group(1).strip(), user.group(1).strip("\n"))
    return sections


def build_retry_messages(kind: str, errors: list[str]) -> list[dict]:
    """重试反馈消息：kind 为 json_unparseable（JSON 无法解析）或 schema_invalid（schema 校验失败）。"""
    try:
        assistant_t, user_t = _retry_sections()[kind]
    except KeyError:
        raise KeyError(f"未知重试反馈类型: {kind}（可选：{sorted(_retry_sections())}）") from None
    ctx = {"errors": "\n".join(errors)}
    return [
        {"role": "assistant", "content": fill_template(assistant_t, ctx)},
        {"role": "user", "content": fill_template(user_t, ctx)},
    ]
