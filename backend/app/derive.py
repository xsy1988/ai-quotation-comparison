"""派生重算（落库前确定性规则）：脚本按规则修正 LLM 版面解析输出，汇总字段不信 LLM 算术。

规则（产品决策，执行顺序固定）：
A. 共享单元格去重：不同条目同引一个 evidence.location 且金额相等、金额文本在每个条目
   的 evidence.raw_text 中恰好出现一次（词边界口径）→ 候选共享组。加严判据：组内任一
   科目名是另一科目名的片段（换行表头拆出的伪列特征，如"镭雕破氧白"拆出"镭雕"/"破氧白"），
   或科目名未真实出现在带文本的 raw_text 中（张冠李戴信号）→ 判不准：不置零，两条目
   都保留原值，写 shared_cell_ambiguous 冲突并给 offer 打 calc_abnormal（交人工，宁可
   重复也不静默丢钱）。判为真共享时按语义优先级选 keeper：科目名含检验类关键词（检）的
   条目在检验费模块优先，其次模块顺序（materials < processing < inspection <
   packaging_transport < sga_tax < other）再按条目索引；其余 amount_per_pc 置 0 并
   note 注明共享。全库 Σitems / 未税总额一律按去重后金额计算。
B. 无出处的模块 total：total 与 Σitems（去重后）冲突时，若提供 ir（IR 对象或 dict，
   取全部单元格数值集合），total 值在 IR 中存在（容差 0.01）→ 单据印有该合计，保留
   total + cross_validation_conflict；不存在 → LLM 推算值，total 改为 Σitems，
   _derived.corrected_module_totals 记录"原 total X 在单据中无出处，已按明细重算" +
   cross_validation_conflict。ir=None → 保守保留 + flag。
C. 无出处的税费金额视为缺失：税费条目 amount_per_pc 为 0 且 rate 非空 → 业务上不存在
   税额为 0 的单据，一律视为金额缺失，按 rate×未税派生覆盖；amount_per_pc 非 null 但
   该金额文本（词边界口径）未出现在自身 evidence.raw_text 中 → 抽取幻觉，走缺失路径：
   有 rate 则派生 amount = rate×未税（去重后明细口径），note 注明"原抽取值 X 与出处
   不符，按税率×未税派生"；无 rate 则保留原值 + cross_validation_conflict。有出处的
   税费金额单据优先，不符只打标。
D. 材料栏只印要素不印材料费时的兜底（勾稽背书，执行于 A 之后、未税/税费计算之前）：
   materials 条目金额未印出（null）而 note/evidence 印出「料价」（每件材料单价，如
   "料重 28；料价 0.96"）→ 料价即候选；候选须通过勾稽背书（候选计入未税后按税费条目
   现值估算税额，含税与单据 final_unit_price_taxed 之差 ≤ 0.05 元/pcs）才照抄为材料费：
   amount_per_pc=候选、note 追加派生标注、_derived.material_price_fallback 记录一次，
   并从 modules_with_missing_amounts 摘掉 materials（否则残留 amount_missing 冲突与
   calc_abnormal）。背书不通过或材料栏无料价 → 保持 null；有候选但背书不通过时写
   material_price_uncorroborated 冲突说明拒绝原因。料价可能只是元/kg 单价（如
   "材料单价 35"），故一律要求背书，绝不出现 料重×料价 这类乘积。
E. 起订量（MOQ）兜底识别：basic.moq 为 null（LLM 未给出）时，扫描 other_info
   （「其它信息」Markdown）与各费用条目/模治具条目的 note、name 文本，按关键词锚点规则
   识别起订量——声明式（MOQ：3K、起订量 2000、起订量不足500）与阈值式
   （订单量少于2000PCS加收开机费）两类写法，K/千=1000、万=10000。命中即写 basic.moq，
   并在 _derived.moq_fallback 归档出处片段与命中文本；识别不到保持 null（不猜测）。
   LLM 已给出的值优先，本规则只兜底不覆盖；规则不读 ir，因此 persist 的"无 IR 重放"
   路径同样生效（幂等：第二次调用 basic.moq 非空即返回）。

随后 summary 全量重算：untaxed_total = 未税总额（一律按去重后明细金额：
materials/processing/inspection/packaging_transport/other 条目 + sga_tax 非税费条目，
不用模块 total 字段）；tax_amount = 税费条目金额（单据值/派生值）；taxed_total = 未税+税额；
final_unit_price_taxed / discount 保留单据值不动，不一致追加 cross_validation_conflict。
LLM 原 summary 存档 offer["_derived"]["summary_llm_original"]。

幂等：重复调用输出一致——重算基于自身输出仍是同一值，归档类字段（LLM 原 summary、
派生标记、共享单元格记录、料价兜底记录、total 修正记录）只写一次，冲突明细每次确定性重放
（规则 D 的拒绝分支不带状态：候选与背书判据都由自身输出重算，故重跑一致）。
"""

import copy
import re
from typing import Any

from app.normalize import amount_in_text as _amount_in_text
from app.normalize import amount_occurrences as _amount_occurrences
from app.normalize import extract_moq
from app.normalize import extract_moq_options
from app.normalize import normalize_moq_value

MODULES = ("materials", "processing", "inspection", "packaging_transport", "sga_tax", "other")
"""参与规则 B 模块 total 判定的费用模块（同 RULE A 的模块顺序）。"""

UNTAXED_MODULES = ("materials", "processing", "inspection", "packaging_transport", "other")
"""未税总额构成模块（sga_tax 只取其中非税费条目金额，税费本身不计入未税）。"""

TAX_ITEM_TYPE = "税费"
"""sga_tax 中税费条目的 item_type。"""

DERIVED_TAX_NOTE = "派生值：税率×未税"
"""税费金额缺失、按税率派生时写入 item note 的标注。"""

MATERIAL_MODULE = "materials"
MATERIAL_PRICE_NOTE = "派生值：料价照抄为每件材料费（勾稽背书通过）"
"""规则 D 兜底填材料费时写入 item note 的标注。"""
MATERIAL_PRICE_TOLERANCE = 0.05
"""规则 D 勾稽背书允差（元/pcs，与单据未税↔含税的 0.05 元勾稽口径一致）。"""

MOQ_FALLBACK_NOTE = "派生值：由「其它信息」/备注文本识别起订量"
"""规则 E 兜底识别起订量时写入 _derived 的说明。"""

MOQ_PRIMARY_FROM_OPTIONS_NOTE = "派生值：由起订量分档取通用（不限条件）档"
"""LLM 只给了分档、漏了主起订量时，回填 basic.moq 的说明。"""

CONFLICT_FLAG = "cross_validation_conflict"
SHARED_CELL_FLAG = "shared_cell"
CALC_ABNORMAL_FLAG = "calc_abnormal"
"""共享单元格判不准（不置零、交人工）时给 offer 打的标记。"""

INSPECTION_MODULE = "inspection"
INSPECTION_NAME_KEYWORD = "检"
"""检验类科目名关键词（检/全检/检验 均含"检"），用于共享单元格 keeper 语义优先级。"""

_TEXT_BEARING_RE = re.compile(r"[A-Za-z一-鿿]")

_MATERIAL_PRICE_RE = re.compile(r"(?:料价|材料单价|料单价|材料价)\s*[:：=]?\s*(\d+(?:\.\d+)?)")
_WEIGHT_RE = re.compile(r"(?:料重|单重|净重|重量)\s*[:：=]?\s*(\d+(?:\.\d+)?)")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
"""规则 D 的料价/料重提取（只在 item.note 与 evidence.raw_text 上做，不碰源文件）。"""


def _items_sum(items: list[dict]) -> float:
    """明细之和（去重后口径）：bundle_flag=true 的行整行计入，不拆分；amount 为 null 视 0。

    null 表示「单据未印出该金额」，此时合计只是下限（见 _missing_amount_count：
    这类模块会记 amount_missing 冲突，不静默当成完整合计）。
    """
    return round(sum(float(item.get("amount_per_pc") or 0) for item in items), 6)


def _missing_amount_count(module_name: str, items: list[dict]) -> int:
    """金额未印出（null）的明细数；税费条目允许 null（由脚本按税率×未税派生），不计入。"""
    return sum(
        1
        for item in items
        if item.get("amount_per_pc") is None
        and not (module_name == "sga_tax" and item.get("item_type") == TAX_ITEM_TYPE)
    )


def _tolerance(expected: float) -> float:
    return max(0.01, abs(expected) * 0.01)


def _appears_exactly_once(raw_text: str | None, amount: float) -> bool:
    """金额文本在 raw_text 中恰好出现一次（出处判定，词边界口径）。"""
    return _amount_occurrences(raw_text, amount) == 1


def _provenance_in_raw(raw_text: str | None, amount: float) -> bool:
    """金额文本是否出现在 raw_text 中（出处判定，不限制次数，词边界口径）。"""
    return _amount_in_text(raw_text, amount)


def _collapse(text: str) -> str:
    """去全部空白（PDF 拆行/拆列文本比对用）。"""
    return re.sub(r"\s+", "", text)


def _shared_group_decision(members: list[tuple[int, int, dict]]) -> str | None:
    """同 location 候选组判定：None=不满足共享前提；'dedupe'=真共享；'ambiguous'=判不准。

    判不准（ambiguous）特征：科目名未真实出现在带文本的 raw_text 中（张冠李戴信号；
    纯数字的裸金额出处无法校验科目名，不据此判不准），或组内一个科目名是另一个的片段
    （换行表头拆出的伪列特征）。validators 的 L2 同口径复用本函数。
    """
    if len(members) < 2:
        return None
    amounts = [m[2].get("amount_per_pc") for m in members]
    if any(a is None for a in amounts) or len({float(a) for a in amounts}) != 1:
        return None
    amount = float(amounts[0])
    # 金额文本须在每个条目的 raw_text 中恰好出现一次，才可能是同一单元格被复制
    if not all(
        _appears_exactly_once((m[2].get("evidence") or {}).get("raw_text"), amount)
        for m in members
    ):
        return None
    names = [_collapse(str(m[2].get("name") or "")) for m in members]
    raws = [_collapse((m[2].get("evidence") or {}).get("raw_text") or "") for m in members]
    if any(not name for name in names):
        return "ambiguous"
    if any(_TEXT_BEARING_RE.search(raw) and name not in raw for name, raw in zip(names, raws)):
        return "ambiguous"
    if any(i != j and names[i] in names[j] for i in range(len(names)) for j in range(len(names))):
        return "ambiguous"
    return "dedupe"


def _shared_group_keeper(members: list[tuple[int, int, dict]]) -> tuple[int, int, dict]:
    """keeper 语义优先级：检验类科目在检验费模块 > 模块顺序 > 条目索引。

    （完整科目名 > 词根碎片：碎片组在 _shared_group_decision 已判 ambiguous，到不了这里。）
    """
    inspection_index = MODULES.index(INSPECTION_MODULE)

    def _priority(member: tuple[int, int, dict]) -> tuple[int, int, int]:
        module_index, item_index, item = member
        name = str(item.get("name") or "")
        inspection = int(module_index == inspection_index and INSPECTION_NAME_KEYWORD in name)
        return (inspection, -module_index, -item_index)

    return max(members, key=_priority)


def _dedupe_shared_cells(offer: dict, derived: dict, conflicts: list[dict]) -> bool:
    """规则 A：共享单元格去重（见模块 docstring）。记录写 _derived.shared_cells（只增一次）；
    判不准的组不置零、写 shared_cell_ambiguous 冲突，返回是否有判不准组。"""
    up = offer["unit_price"]
    groups: dict[str, list[tuple[int, int, dict]]] = {}
    for module_index, name in enumerate(MODULES):
        for item_index, item in enumerate((up.get(name) or {}).get("items") or []):
            location = (item.get("evidence") or {}).get("location")
            if location:
                groups.setdefault(location, []).append((module_index, item_index, item))

    records = derived.setdefault("shared_cells", [])
    recorded = {(r["location"], r["kept"]) for r in records}
    ambiguous = False
    for location, members in groups.items():
        decision = _shared_group_decision(members)
        if decision is None:
            continue
        if decision == "ambiguous":
            ambiguous = True
            names = "、".join(str(m[2].get("name")) for m in members)
            amount = float(members[0][2]["amount_per_pc"])
            conflicts.append(
                {
                    "kind": "shared_cell_ambiguous",
                    "module": "unit_price",
                    "document_value": amount,
                    "derived_value": None,
                    "detail": f"条目 {names} 同引单元格 {location} 且金额相同 {amount}，"
                              f"但科目名出处/表头碎片特征判不准是否真共享，保留原值待人工核对",
                }
            )
            continue
        keeper = _shared_group_keeper(members)
        kept_item = keeper[2]
        zeroed_names = []
        for _mi, _ii, item in members:
            if item is kept_item:
                continue
            item["amount_per_pc"] = 0.0
            note = item.get("note")
            marker = f"与『{kept_item.get('name')}』共享单元格 {location}，金额只计一次"
            item["note"] = f"{note}；{marker}" if note else marker
            zeroed_names.append(item.get("name"))
        if (location, kept_item.get("name")) not in recorded:
            records.append(
                {"location": location, "kept": kept_item.get("name"), "zeroed": zeroed_names}
            )
    return ambiguous


def _ir_numeric_values(ir) -> set[float] | None:
    """从 IR 对象或 dict 提取全部单元格数值集合；ir=None 返回 None（保守路径）。"""
    if ir is None:
        return None
    if isinstance(ir, dict):
        tables = ir.get("tables") or []
    else:
        tables = getattr(ir, "tables", None) or []
    values: set[float] = set()
    for table in tables:
        rows = table.get("rows") if isinstance(table, dict) else getattr(table, "rows", None)
        if rows is None:  # 兼容 {"tables": [{"cells": [...]}]} 拍平结构
            rows = [table]
        for row in rows:
            cells = row.get("cells") if isinstance(row, dict) else getattr(row, "cells", None)
            for cell in cells or []:
                value = cell.get("value") if isinstance(cell, dict) else getattr(cell, "value", None)
                if value is None:
                    continue
                try:
                    values.add(round(float(str(value).strip()), 6))
                except ValueError:
                    continue
    return values


def _has_provenance_in_ir(ir_values: set[float] | None, total: float) -> bool:
    """total 值是否在 IR 单元格数值中存在（容差 0.01）。"""
    if ir_values is None:
        return False
    return any(abs(v - total) <= 0.01 for v in ir_values)


def _fix_module_totals(offer: dict, derived: dict, conflicts: list[dict],
                       ir_values: set[float] | None) -> None:
    """规则 B：逐模块修正 total（冲突时按 IR 出处判定保留或重算；null 回填 Σitems）。

    带 ir 判过"单据印有该合计"的模块记入 _derived.provenance_totals，后续保守重跑
    （persist 不传 ir）维持原判定，保证双跑记录一致。
    """
    up = offer["unit_price"]
    filled = derived.setdefault("filled_module_totals", [])
    corrected = derived.setdefault("corrected_module_totals", {})
    provenance = derived.setdefault("provenance_totals", {})
    # 金额未印出（null）的模块：合计只能算下限，记成冲突并持久化（重放保证双跑一致）
    missing_amounts = derived.setdefault("modules_with_missing_amounts", {})
    for name in MODULES:
        module = up.get(name) or {}
        items = module.get("items") or []
        total = module.get("total")
        if items:
            missing = _missing_amount_count(name, items)
            if missing:
                missing_amounts[name] = missing
        if total is None:
            # 全明细金额都未印出（null）时 Σitems 恒为 0、没有任何已知项，写回就是拿 0 占位
            # （v1.4 null 纪律），此时保持 null；只要有已知项，按已知项之和作下限回填。
            if items and any(item.get("amount_per_pc") is not None for item in items):
                module["total"] = _items_sum(items)
                if name not in filled:
                    filled.append(name)
            continue
        if not items:
            continue
        items_sum = _items_sum(items)
        if any(abs(float(total) - value) <= _tolerance(value) for value in _total_alternatives(items, items_sum)):
            if name in corrected:
                # 上次调用已修正（total 现为 Σitems，无活冲突）：重放冲突明细，保证幂等
                conflicts.append(
                    {
                        "kind": "module_total",
                        "module": name,
                        "document_value": corrected[name]["original_total"],
                        "derived_value": corrected[name]["items_sum"],
                        "detail": f"{name} 原 total {corrected[name]['original_total']} 在单据中无出处，已按明细重算为 {corrected[name]['items_sum']}",
                    }
                )
            continue
        if ir_values is not None and _has_provenance_in_ir(ir_values, float(total)):
            # 单据印有该合计：单据优先，保留原值
            provenance.setdefault(name, {"document_value": float(total), "items_sum": items_sum})
            conflicts.append(
                {
                    "kind": "module_total",
                    "module": name,
                    "document_value": float(total),
                    "derived_value": items_sum,
                    "detail": f"{name} total={total} 与 Σitems={items_sum} 不符（单据印有该合计），保留单据值",
                }
            )
        elif name in provenance:
            # 此前（带 ir）已判定单据印有该合计：保守重跑维持原判定
            info = provenance[name]
            conflicts.append(
                {
                    "kind": "module_total",
                    "module": name,
                    "document_value": info["document_value"],
                    "derived_value": info["items_sum"],
                    "detail": f"{name} total={info['document_value']} 与 Σitems={info['items_sum']} 不符（单据印有该合计），保留单据值",
                }
            )
        elif ir_values is not None:
            # IR 中无此数：LLM 推算值，按明细（去重后）重算
            module["total"] = items_sum
            if name not in corrected:
                corrected[name] = {"original_total": float(total), "items_sum": items_sum}
            conflicts.append(
                {
                    "kind": "module_total",
                    "module": name,
                    "document_value": float(total),
                    "derived_value": items_sum,
                    "detail": f"{name} 原 total {total} 在单据中无出处，已按明细重算为 {items_sum}",
                }
            )
        else:
            # 无 IR（保守路径）：保留单据值
            conflicts.append(
                {
                    "kind": "module_total",
                    "module": name,
                    "document_value": float(total),
                    "derived_value": items_sum,
                    "detail": f"{name} total={total} 与 Σitems={items_sum} 不符，保留单据值",
                }
            )
    for name, count in missing_amounts.items():
        # 幂等重放（与 corrected/provenance 同思路）：每轮都从持久化的记录里重放冲突
        module = up.get(name) or {}
        total = module.get("total")
        lower_bound = (
            f"合计 {total} 只是下限"
            if total is not None
            else "明细金额全部未印出，无可信合计（保持 null）"
        )
        conflicts.append(
            {
                "kind": "amount_missing",
                "module": name,
                "document_value": None,
                "derived_value": total,
                "detail": f"{name} 有 {count} 项明细金额单据未印出（null，Σitems 按 0 计入），"
                          f"{lower_bound}",
            }
        )


def _total_alternatives(items: list[dict], items_sum: float) -> list[float]:
    """模块 total 可接受的"明细和"口径候选。

    sga_tax 的 total 常是"损管利税合计"（不含单列的税费），此时 total≠Σitems（Σitems 含税费）
    属正常印法，不应记成 module_total 冲突（曾据此误报）。其余模块只有 Σitems 一种口径。
    """
    alternatives = [items_sum]
    if any(item.get("item_type") == TAX_ITEM_TYPE for item in items):
        non_tax = _items_sum([item for item in items if item.get("item_type") != TAX_ITEM_TYPE])
        if non_tax != items_sum:
            alternatives.append(non_tax)
    return alternatives


def _module_contribution(up: dict, name: str) -> float:
    """模块对未税总额的贡献：有明细按去重后明细金额（不用 total 字段，避免未修正的 total
    污染税额派生）；无明细的模块回退 total（唯一信息源，无去重/修正风险）。"""
    module = up.get(name) or {}
    items = module.get("items") or []
    if items:
        return _items_sum(items)
    return round(float(module["total"]), 6) if module.get("total") is not None else 0.0


def untaxed_total_of(up: dict) -> float:
    """未税总额：未税模块贡献 + sga_tax 的非税费贡献。

    derive 重算 summary 与 validate.calc_check 校验 summary 必须共用本函数：两个口径各算一套
    是勾稽校验误报的根源（同一份单据被算出两个未税总额）。
    """
    return round(
        sum(_module_contribution(up, name) for name in UNTAXED_MODULES)
        + sga_untaxed_contribution(up.get("sga_tax") or {}),
        6,
    )


def untaxed_total_candidates(up: dict) -> list[float]:
    """未税总额的可接受口径候选：明细口径优先，其次"单据模块 total"口径。

    两套口径都是单据的真实印法（明细逐项列出 vs 只给模块合计），而人工修正的正是模块 total
    （correction_service 以 total 为准重算），所以校验只要求 summary 落在任一口径上即可，
    不能只认其中一个（曾据此误报 fail 与 module_total 冲突）。
    """
    items_based = untaxed_total_of(up)

    def _total_of(name: str) -> float:
        module = up.get(name) or {}
        if module.get("total") is not None:
            return float(module["total"])
        items = module.get("items") or []
        return _items_sum(items) if any(i.get("amount_per_pc") is not None for i in items) else 0.0

    totals_based = sum(_total_of(name) for name in UNTAXED_MODULES)
    sga = up.get("sga_tax") or {}
    if sga.get("total") is not None:
        tax_sum = sum(
            float(item["amount_per_pc"])
            for item in sga.get("items") or []
            if item.get("item_type") == TAX_ITEM_TYPE and item.get("amount_per_pc") is not None
        )
        totals_based += float(sga["total"]) - tax_sum
    else:
        totals_based += sga_untaxed_contribution(sga)
    totals_based = round(totals_based, 6)
    return [items_based] if abs(totals_based - items_based) <= 1e-9 else [items_based, totals_based]


def sga_untaxed_contribution(sga: dict) -> float:
    """sga_tax 模块对未税总额的贡献（全库唯一口径，derive 与 validate.calc_check 共用）。

    单据对"损管利税"这块的印法不统一：有的把"损管利税 2.85"与"增值税 2.04"分两行印（total
    不含税费），有的把税费并进 total。统一取：有非税费明细 → Σ非税费明细；只有税费明细 →
    total − Σ税费明细（total 缺失记 0）；无任何明细 → total（唯一信息源，同 _module_contribution）。
    此前 derive 按明细、calc_check 按 total−税费，同一份单据被两个口径读出两个未税总额，
    勾稽校验误报失败。
    """
    items = sga.get("items") or []
    non_tax = [
        float(item["amount_per_pc"])
        for item in items
        if item.get("item_type") != TAX_ITEM_TYPE and item.get("amount_per_pc") is not None
    ]
    if non_tax:
        return round(sum(non_tax), 6)
    tax_sum = sum(
        float(item["amount_per_pc"])
        for item in items
        if item.get("item_type") == TAX_ITEM_TYPE and item.get("amount_per_pc") is not None
    )
    total = sga.get("total")
    if total is None:
        return 0.0
    return round(float(total) - tax_sum, 6)


def _tax_item_of(up: dict) -> dict | None:
    return next(
        (
            item
            for item in (up.get("sga_tax") or {}).get("items") or []
            if item.get("item_type") == TAX_ITEM_TYPE
        ),
        None,
    )


def _estimated_tax(up: dict, untaxed: float) -> float | None:
    """按税费条目现值估算税额（规则 D 勾稽用，不改数据）：有出处的单据值优先，否则 rate×未税。"""
    item = _tax_item_of(up)
    if item is None:
        return None
    amount = item.get("amount_per_pc")
    rate = item.get("rate")
    raw_text = (item.get("evidence") or {}).get("raw_text")
    if amount is not None and float(amount) != 0 and (
        not raw_text or _provenance_in_raw(raw_text, float(amount))
    ):
        return float(amount)
    if rate is not None:
        return round(float(rate) * untaxed, 6)
    return None


def _material_price_candidate(item: dict) -> float | None:
    """提取材料条目印出的「料价」（每件材料费候选），提取不到返回 None。

    优先 note 中的料价/材料单价关键词；退一步：note 印了料重且 evidence.raw_text 恰好
    两个数（料重/料价两列）→ 取非料重的那个。料价是否真是每件材料费由勾稽背书判定。
    """
    note = item.get("note") or ""
    match = _MATERIAL_PRICE_RE.search(note)
    if match and float(match.group(1)) > 0:
        return float(match.group(1))
    weight_match = _WEIGHT_RE.search(note)
    raw_text = (item.get("evidence") or {}).get("raw_text") or ""
    numbers = [float(number) for number in _NUMBER_RE.findall(raw_text)]
    if weight_match and len(numbers) == 2:
        others = [number for number in numbers if abs(number - float(weight_match.group(1))) > 1e-9]
        if len(others) == 1 and others[0] > 0:
            return others[0]
    return None


def _moq_fallback(offer: dict, derived: dict) -> None:
    """规则 E：basic.moq / moq_options 缺失时，从「其它信息」与条目备注/名称中兜底识别起订量。"""
    basic = offer.get("basic")
    if not isinstance(basic, dict):
        return
    if basic.get("moq") is None:
        for text in _moq_texts(offer):
            found = extract_moq(text)
            if found is None:
                continue
            value, snippet = found
            basic["moq"] = value
            derived["moq_fallback"] = {
                "value": value,
                "snippet": snippet,
                "text": text if len(text) <= 200 else text[:200] + "…",
                "note": MOQ_FALLBACK_NOTE,
            }
            break
    if basic.get("moq_options") is None:
        for text in _moq_texts(offer):
            options = extract_moq_options(text)
            # 单条且不限条件 = 普通起订量，交给 moq 即可，不必再立分档
            if not options or (len(options) == 1 and not options[0]["condition"]):
                continue
            basic["moq_options"] = [
                {"condition": item["condition"], "value": item["value"], "note": None}
                for item in options
            ]
            derived["moq_options_fallback"] = {
                "count": len(options),
                "snippet": "；".join(item["snippet"] for item in options),
                "text": text if len(text) <= 200 else text[:200] + "…",
                "note": MOQ_FALLBACK_NOTE,
            }
            break


def _clean_moq_options(value: Any) -> list[dict[str, Any]]:
    """规范化 LLM 给出的 moq_options：丢弃无有效数值的条目、按「条件+数值」去重，保留有效条目。"""
    if not isinstance(value, list):
        return None
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple[str | None, int]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        fetched = normalize_moq_value(item.get("value"))
        if fetched is None:
            continue
        condition = item.get("condition")
        condition = condition.strip() if isinstance(condition, str) and condition.strip() else None
        note = item.get("note")
        note = note.strip() if isinstance(note, str) and note.strip() else None
        if (condition, fetched) in seen:
            continue
        seen.add((condition, fetched))
        cleaned.append({"condition": condition, "value": fetched, "note": note})
    return cleaned


def _is_tiered_moq(options: list[dict[str, Any]]) -> bool:
    """是否真的算「分档」：多档，或单档但带适用条件（单档且不限条件等同普通起订量）。"""
    return len(options) > 1 or options[0]["condition"] is not None


def _normalize_moq_options(basic: dict, derived: dict) -> None:
    """清理 moq_options；moq 缺失时用通用（不限条件）档回填；不成档的降级为 None 只留 moq。"""
    options = _clean_moq_options(basic.get("moq_options"))
    if options and basic.get("moq") is None:
        primary = next((item for item in options if item["condition"] is None), options[0])
        basic["moq"] = primary["value"]
        derived["moq_primary_from_options"] = {
            "value": primary["value"],
            "condition": primary["condition"],
            "note": MOQ_PRIMARY_FROM_OPTIONS_NOTE,
        }
    basic["moq_options"] = options if options and _is_tiered_moq(options) else None


def _moq_texts(offer: dict) -> list[str]:
    """规则 E 的检索文本：其它信息在前（商务条款通常写在这里），其后是条目备注/名称。"""
    texts: list[str] = []
    other_info = offer.get("other_info")
    if isinstance(other_info, str) and other_info.strip():
        texts.append(other_info)
    tooling = offer.get("tooling") or {}
    for section in ("molds", "fixtures", "stencils"):
        texts.extend(_item_texts((tooling.get(section) or {}).get("items")))
    up = offer.get("unit_price") or {}
    for name in MODULES:
        texts.extend(_item_texts((up.get(name) or {}).get("items")))
    return texts


def _item_texts(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    texts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for field in ("note", "name"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def _fill_material_prices(offer: dict, derived: dict, conflicts: list[dict]) -> None:
    """规则 D：材料栏只印要素不印材料费时，勾稽背书通过则把料价照抄为材料费。"""
    up = offer["unit_price"]
    materials = up.get(MATERIAL_MODULE) or {}
    items = materials.get("items") or []
    if materials.get("total") is not None or not items:
        return  # 单据印有材料费合计（归规则 B 处理）或材料栏无明细
    pending = [
        (index, item) for index, item in enumerate(items) if item.get("amount_per_pc") is None
    ]
    if not pending:
        return
    found = {
        index: value
        for index, item in pending
        if (value := _material_price_candidate(item)) is not None
    }
    if not found:
        return
    candidate_sum = round(sum(found.values()), 6)
    summary = up.get("summary") or {}
    anchor = summary.get("final_unit_price_taxed")
    untaxed_with = round(untaxed_total_of(up) + candidate_sum, 6)
    tax = _estimated_tax(up, untaxed_with)
    expected = (
        round(untaxed_with + tax - float(summary.get("discount") or 0), 6)
        if tax is not None and anchor is not None
        else None
    )
    # 允差比较前 round 到 6 位：两值本身均已 round，差值需消掉浮点噪声（18.645−18.595
    # 浮点结果为 0.0500000000000007，按 ≤0.05 的口径应视为临界通过）
    if expected is not None and round(abs(expected - float(anchor)), 6) <= MATERIAL_PRICE_TOLERANCE:
        for index, value in found.items():
            item = items[index]
            note = item.get("note")
            item["amount_per_pc"] = value
            item["note"] = f"{note}；{MATERIAL_PRICE_NOTE}" if note else MATERIAL_PRICE_NOTE
        # 归档只写一次（重跑时材料费已填，本规则提前返回）
        derived.setdefault("material_price_fallback", []).append(
            {
                "module": MATERIAL_MODULE,
                "value": candidate_sum,
                "items": len(found),
                "document_final_unit_price_taxed": float(anchor),
                "derived_taxed_total": expected,
                "detail": f"材料栏未印材料费，料价 {candidate_sum} 照抄为每件材料费"
                          f"（计入后含税 {expected} 与单据含税单价 {anchor} 勾稽一致）",
            }
        )
        # 金额已补齐：摘掉持久化的「金额未印出」记录，否则会残留 amount_missing 冲突
        derived.get("modules_with_missing_amounts", {}).pop(MATERIAL_MODULE, None)
        return
    if expected is None:
        reason = f"材料栏未印材料费，料价候选 {candidate_sum} 无法勾稽背书（缺单据含税单价或税费条目）"
    else:
        reason = (
            f"材料栏未印材料费，料价候选 {candidate_sum} 勾稽不通过"
            f"（计入后含税 {expected} vs 单据含税单价 {anchor}，允差 {MATERIAL_PRICE_TOLERANCE}）"
        )
    conflicts.append(
        {
            "kind": "material_price_uncorroborated",
            "module": MATERIAL_MODULE,
            "document_value": candidate_sum,
            "derived_value": None,
            "detail": f"{reason}，保持未印出（null）",
        }
    )


def _resolve_summary_tax(
    offer: dict, derived: dict, conflicts: list[dict], untaxed: float, ir_values: set[float] | None
) -> float | None:
    """无 sga_tax 税费条目时：用单据汇总行印出的税额兜底（如「未税合计 13.66 / 增值税 1.78 /
    含税单价 15.44」三行，行首栏目名单元格为空，版面理解把它们放进 summary 而不是 sga_tax 明细）。

    此前只认 sga_tax 条目：印出来的税额被丢弃 → 含税合计算不出来、final_price 与勾稽校验
    双双误报失败（单据 10 号实例）。采纳条件（保守，宁缺勿造）：
    * 候选值 = 版面理解读到的 summary.tax_amount，且 > 0；
    * 有 IR 时必须在 IR 数值中存在（容差 0.01）——即"单据上确实印了这个数"；
    * 无 IR（回放/手工单据）时要求单据自身闭合：含税合计 − 未税合计 ≈ 候选值。
    """
    summary = (offer.get("unit_price") or {}).get("summary") or {}
    try:
        candidate = float(summary.get("tax_amount"))
    except (TypeError, ValueError):
        return None
    if candidate <= 0:
        return None
    if ir_values is not None:
        if not _has_provenance_in_ir(ir_values, candidate):
            conflicts.append(
                {
                    "kind": "tax_amount",
                    "module": "sga_tax",
                    "document_value": candidate,
                    "derived_value": None,
                    "detail": f"汇总行税额 {candidate} 在单据中无出处，保持未印出（null）",
                }
            )
            return None
    else:
        taxed = summary.get("taxed_total")
        try:
            closed = taxed is not None and abs((float(taxed) - untaxed) - candidate) <= _tolerance(candidate)
        except (TypeError, ValueError):
            closed = False
        if not closed:
            return None
    derived["summary_tax_amount"] = candidate
    return round(candidate, 6)


def _resolve_tax(offer: dict, derived: dict, conflicts: list[dict], untaxed: float,
                 ir_values: set[float] | None = None) -> float | None:
    """规则 C + 税费决策：无出处金额视为缺失按税率派生；有出处单据值优先（不符只打标）。"""
    up = offer["unit_price"]
    sga_items = (up.get("sga_tax") or {}).get("items") or []
    tax_item = next((i for i in sga_items if i.get("item_type") == TAX_ITEM_TYPE), None)
    newly_derived = False
    tax_amount: float | None = None
    already_derived = bool(derived.get("tax_derived"))
    if already_derived:
        # 已派生税费：金额视为终值，只参与 summary 重算（派生值=rate×未税，天然无冲突）
        if tax_item is not None and tax_item.get("amount_per_pc") is not None:
            tax_amount = float(tax_item["amount_per_pc"])
        elif tax_item is not None and tax_item.get("rate") is not None:
            tax_amount = round(float(tax_item["rate"]) * untaxed, 6)
            tax_item["amount_per_pc"] = tax_amount
        else:
            tax_amount = derived.get("summary_tax_amount")  # 回放：沿用首轮从汇总行采纳的税额
        derived["tax_derived"] = True
        return tax_amount
    if tax_item is None:
        return _resolve_summary_tax(offer, derived, conflicts, untaxed, ir_values)
    if tax_item.get("amount_per_pc") is not None:
        amount = float(tax_item["amount_per_pc"])
        raw_text = (tax_item.get("evidence") or {}).get("raw_text")
        if amount == 0 and tax_item.get("rate") is not None:
            # 业务上不存在税额真为 0 的单据：0 一律视为金额缺失（LLM 误填），按税率×未税派生
            tax_amount = round(float(tax_item["rate"]) * untaxed, 6)
            tax_item["amount_per_pc"] = tax_amount
            tax_item["note"] = f"原抽取值 {amount} 视为税额缺失，按税率×未税派生"
            newly_derived = True
        elif raw_text and not _provenance_in_raw(raw_text, amount):
            # 有出处可核验但金额不在其中：抽取幻觉（如抄相邻单元格），按税率×未税派生；
            # 无 evidence 的手工/回放单据不判幻觉，走单据值优先
            if tax_item.get("rate") is not None:
                tax_amount = round(float(tax_item["rate"]) * untaxed, 6)
                tax_item["amount_per_pc"] = tax_amount
                tax_item["note"] = f"原抽取值 {amount} 与出处不符，按税率×未税派生"
                newly_derived = True
            else:
                tax_amount = amount
                conflicts.append(
                    {
                        "kind": "tax_amount",
                        "module": "sga_tax",
                        "document_value": amount,
                        "derived_value": None,
                        "detail": f"税费金额 {amount} 在单据中无出处且无税率可派生，保留原值",
                    }
                )
        else:
            # 单据值优先，与 税率×未税 不符只打标不改值
            tax_amount = amount
            rate = tax_item.get("rate")
            if rate is not None:
                expected = float(rate) * untaxed
                if abs(tax_amount - expected) > _tolerance(expected):
                    conflicts.append(
                        {
                            "kind": "tax_amount",
                            "module": "sga_tax",
                            "document_value": tax_amount,
                            "derived_value": round(expected, 6),
                            "detail": f"税费金额 {tax_amount} 与 税率×未税 {round(expected, 6)} 不符，保留单据值",
                        }
                    )
    elif tax_item is not None and tax_item.get("rate") is not None:
        # amount 缺失：按税率派生
        tax_amount = round(float(tax_item["rate"]) * untaxed, 6)
        tax_item["amount_per_pc"] = tax_amount
        note = tax_item.get("note")
        tax_item["note"] = f"{note}；{DERIVED_TAX_NOTE}" if note else DERIVED_TAX_NOTE
        newly_derived = True
    if newly_derived or already_derived:
        derived["tax_derived"] = True
    return tax_amount


def derive_offer(offer: dict, ir=None) -> list[str]:
    """对单个 offer 应用派生重算规则。

    顺序：A 去重 → D 料价兜底（勾稽背书）→ E 起订量兜底 → 未税 → C 税费 → B 模块 total → summary 重算。
    ir 可选（IR 对象或 dict）：提供时用于规则 B/C 的"单据出处"判定；不传则保守降级。
    就地修正并返回 flags 增量（shared_cell / cross_validation_conflict / calc_abnormal）。
    """
    up = offer["unit_price"]
    derived = offer.setdefault("_derived", {})
    conflicts: list[dict] = []

    # 规则 A：共享单元格去重（先执行，全库口径按去重后金额）；判不准不置零，打 calc_abnormal
    ambiguous_shared = _dedupe_shared_cells(offer, derived, conflicts)

    # 规则 D：材料栏只印料价（未印材料费）时按勾稽背书兜底填材料费（在未税/税费计算之前）
    _fill_material_prices(offer, derived, conflicts)

    # 规则 E：起订量缺失时从「其它信息」/备注文本兜底识别（不参与金额勾稽）
    if isinstance(offer.get("basic"), dict):
        _normalize_moq_options(offer["basic"], derived)
    _moq_fallback(offer, derived)

    # 未税总额：有明细的模块按去重后明细金额（不用 total 字段，避免未修正的 total 污染税额派生）
    untaxed = untaxed_total_of(up)
    ir_values = _ir_numeric_values(ir)

    # 规则 C + 税费决策（sga_tax 的 total 在税费修正后于规则 B 中重算 Σitems）
    tax_amount = _resolve_tax(offer, derived, conflicts, untaxed, ir_values)

    # 规则 B：模块 total（ir 提供时按出处判定；sga_tax 此时已含修正后税费金额）
    _fix_module_totals(offer, derived, conflicts, ir_values)

    # summary 全量重算，final/discount 保留单据值
    summary = up["summary"]
    if "summary_llm_original" not in derived:
        derived["summary_llm_original"] = copy.deepcopy(summary)
    taxed = round(untaxed + (tax_amount or 0), 6)
    summary["untaxed_total"] = untaxed
    summary["tax_amount"] = tax_amount
    summary["taxed_total"] = taxed
    final = summary.get("final_unit_price_taxed")
    if final is not None:
        expected_final = taxed - (summary.get("discount") or 0)
        if abs(float(final) - expected_final) > _tolerance(expected_final):
            conflicts.append(
                {
                    "kind": "final_price",
                    "module": "summary",
                    "document_value": float(final),
                    "derived_value": round(expected_final, 6),
                    "detail": f"final_unit_price_taxed {final} 与 含税−折扣 {round(expected_final, 6)} 不符，保留单据值",
                }
            )

    derived["conflicts"] = conflicts
    flags: list[str] = []
    if derived.get("shared_cells"):
        flags.append(SHARED_CELL_FLAG)
    if conflicts:
        flags.append(CONFLICT_FLAG)
    if ambiguous_shared:
        flags.append(CALC_ABNORMAL_FLAG)
    return flags


def derive_envelope(envelope: dict, ir=None) -> dict:
    """对信封内每个 offer 应用派生重算，返回 {"envelope": ..., "flags_per_offer": [...]}。"""
    offers = envelope.get("offers") or []
    return {
        "envelope": envelope,
        "flags_per_offer": [derive_offer(offer, ir=ir) for offer in offers],
    }
