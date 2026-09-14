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

随后 summary 全量重算：untaxed_total = 未税总额（一律按去重后明细金额：
materials/processing/inspection/packaging_transport/other 条目 + sga_tax 非税费条目，
不用模块 total 字段）；tax_amount = 税费条目金额（单据值/派生值）；taxed_total = 未税+税额；
final_unit_price_taxed / discount 保留单据值不动，不一致追加 cross_validation_conflict。
LLM 原 summary 存档 offer["_derived"]["summary_llm_original"]。

幂等：重复调用输出一致——重算基于自身输出仍是同一值，归档类字段（LLM 原 summary、
派生标记、共享单元格记录、total 修正记录）只写一次，冲突明细每次确定性重放。
"""

import copy
import re

from app.normalize import amount_in_text as _amount_in_text
from app.normalize import amount_occurrences as _amount_occurrences

MODULES = ("materials", "processing", "inspection", "packaging_transport", "sga_tax", "other")
"""参与规则 B 模块 total 判定的费用模块（同 RULE A 的模块顺序）。"""

UNTAXED_MODULES = ("materials", "processing", "inspection", "packaging_transport", "other")
"""未税总额构成模块（sga_tax 只取其中非税费条目金额，税费本身不计入未税）。"""

TAX_ITEM_TYPE = "税费"
"""sga_tax 中税费条目的 item_type。"""

DERIVED_TAX_NOTE = "派生值：税率×未税"
"""税费金额缺失、按税率派生时写入 item note 的标注。"""

CONFLICT_FLAG = "cross_validation_conflict"
SHARED_CELL_FLAG = "shared_cell"
CALC_ABNORMAL_FLAG = "calc_abnormal"
"""共享单元格判不准（不置零、交人工）时给 offer 打的标记。"""

INSPECTION_MODULE = "inspection"
INSPECTION_NAME_KEYWORD = "检"
"""检验类科目名关键词（检/全检/检验 均含"检"），用于共享单元格 keeper 语义优先级。"""

_TEXT_BEARING_RE = re.compile(r"[A-Za-z一-鿿]")


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
        if abs(float(total) - items_sum) <= _tolerance(items_sum):
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


def _resolve_tax(offer: dict, derived: dict, conflicts: list[dict], untaxed: float) -> float | None:
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
        derived["tax_derived"] = True
        return tax_amount
    if tax_item is not None and tax_item.get("amount_per_pc") is not None:
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
    """对单个 offer 应用派生重算规则（顺序：A 去重 → 未税 → C 税费 → B 模块 total → summary）。

    ir 可选（IR 对象或 dict）：提供时用于规则 B/C 的"单据出处"判定；不传则保守降级。
    就地修正并返回 flags 增量（shared_cell / cross_validation_conflict / calc_abnormal）。
    """
    up = offer["unit_price"]
    derived = offer.setdefault("_derived", {})
    conflicts: list[dict] = []

    # 规则 A：共享单元格去重（先执行，全库口径按去重后金额）；判不准不置零，打 calc_abnormal
    ambiguous_shared = _dedupe_shared_cells(offer, derived, conflicts)

    # 未税总额：有明细的模块按去重后明细金额（不用 total 字段，避免未修正的 total 污染税额派生）；
    # 无明细的模块回退 total（唯一信息源，无去重/修正风险）
    def _module_contribution(name: str) -> float:
        module = up.get(name) or {}
        items = module.get("items") or []
        if items:
            return _items_sum(items)
        return round(float(module["total"]), 6) if module.get("total") is not None else 0.0

    untaxed = round(
        sum(_module_contribution(name) for name in UNTAXED_MODULES)
        + _items_sum(
            [
                item
                for item in (up.get("sga_tax") or {}).get("items") or []
                if item.get("item_type") != TAX_ITEM_TYPE
            ]
        ),
        6,
    )

    # 规则 C + 税费决策（sga_tax 的 total 在税费修正后于规则 B 中重算 Σitems）
    tax_amount = _resolve_tax(offer, derived, conflicts, untaxed)

    # 规则 B：模块 total（ir 提供时按出处判定；sga_tax 此时已含修正后税费金额）
    _fix_module_totals(offer, derived, conflicts, _ir_numeric_values(ir))

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
