"""派生重算（app.derive）：共享单元格去重 / 模块 total 出处修正 / 税费幻觉派生 / summary 重算。

金标：惠州市创锋科技双 offer 存档信封 + 源文件 IR（tests/fixtures/ir_chuangfeng.json）。
真实抽取缺陷：换行表头"镭雕破氧白"被拆散成伪列，LLM 把"镭雕0.30"（实为"全检"的金额）
与"全检0.30"都挂上 page_1!R4C18；"包装0.10"与"运输0.10"同引 page_1!R4C20（单据该格
"运输 0.10 包装"一格服务两科目）；税费金额 2.01/1.83 为 LLM 抄相邻"不良率 2.01/1.83"
的幻觉（自身 raw_text 中只有税率）。

derive 后（ir 传入）源文件核对值全量复现：offer1 未税13.93/税1.81/含税15.74，
offer2 未税12.68/税1.65/含税14.33，final 与含税一致无冲突。共享单元格 keeper 按语义
优先级保留"全检"（检验费模块的检验类科目），误植的"镭雕"副本置零——全检 0.30 不再丢失。
"""

import copy
import json
from pathlib import Path

import pytest

from app.derive import (
    CALC_ABNORMAL_FLAG,
    CONFLICT_FLAG,
    DERIVED_TAX_NOTE,
    SHARED_CELL_FLAG,
    derive_envelope,
    derive_offer,
)
from app.normalize import amount_in_text

FIXTURE = Path(__file__).parent / "fixtures" / "envelope_chuangfeng_two_offers.json"
IR_FIXTURE = Path(__file__).parent / "fixtures" / "ir_chuangfeng.json"


def _offer(index: int) -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["offers"][index]


def _ir() -> dict:
    return json.loads(IR_FIXTURE.read_text(encoding="utf-8"))


def _item(offer: dict, module: str, name: str) -> dict:
    return next(i for i in offer["unit_price"][module]["items"] if i["name"] == name)


# ---------------------------------------------------------------------------
# 金标：创锋双 offer（存档信封 + 源文件 IR）
# ---------------------------------------------------------------------------

def test_gold_offer1_shared_cell_dedupe_and_summary():
    offer = _offer(0)
    flags = derive_offer(offer, ir=_ir())
    up = offer["unit_price"]

    # 规则 A：共享单元格去重——keeper 按语义优先级：检验类科目"全检"在检验费模块优先
    # 保留，误植的"镭雕"副本置零；包装/运输 按模块内条目索引保留 包装
    assert _item(offer, "processing", "镭雕")["amount_per_pc"] == 0.0
    assert "与『全检』共享单元格 page_1!R4C18" in (_item(offer, "processing", "镭雕")["note"] or "")
    assert _item(offer, "inspection", "全检")["amount_per_pc"] == 0.3  # 全检 0.30 不再被置零
    assert _item(offer, "packaging_transport", "包装")["amount_per_pc"] == 0.1
    assert _item(offer, "packaging_transport", "运输")["amount_per_pc"] == 0.0
    assert "共享单元格 page_1!R4C20" in (_item(offer, "packaging_transport", "运输")["note"] or "")
    assert SHARED_CELL_FLAG in flags
    # 不同 location 的同值金额（损耗 2.01）不受去重影响
    assert _item(offer, "sga_tax", "不良率损耗")["amount_per_pc"] == 2.01

    # 规则 B：processing total=5.45 在单据中无出处 → 按明细（去重后，镭雕已置零）重算为 6.65；
    # packaging total=0.10 与去重后 Σitems 一致 → 不动
    assert up["processing"]["total"] == 6.65
    assert up["packaging_transport"]["total"] == 0.10
    assert up["processing"]["total"] != 5.45
    assert offer["_derived"]["corrected_module_totals"]["processing"] == {
        "original_total": 5.45,
        "items_sum": 6.65,
    }
    # inspection total=0.30 在单据中有出处 → 保留单据值 + flag
    assert up["inspection"]["total"] == 0.3

    # 规则 C：税费 2.01 无出处（raw_text 只有"税率 13% 13.0%"）→ 按 0.13×未税 派生
    tax_item = _item(offer, "sga_tax", "税费")
    assert tax_item["amount_per_pc"] == pytest.approx(1.81, abs=0.01)
    assert "原抽取值 2.01 与出处不符" in (tax_item["note"] or "")
    assert offer["_derived"]["tax_derived"] is True

    # summary：未税=3.0+6.65+0.3+0.1+0+(2.01+1.21+0.66)=13.93；含税=未税+税额≈15.74；
    # final=15.74 与含税一致 → 无 final 冲突
    assert up["summary"]["untaxed_total"] == pytest.approx(13.93)
    assert up["summary"]["tax_amount"] == pytest.approx(1.81, abs=0.01)
    assert up["summary"]["taxed_total"] == pytest.approx(15.74, abs=0.01)
    assert up["summary"]["final_unit_price_taxed"] == 15.74
    final_conflicts = [c for c in offer["_derived"]["conflicts"] if c["kind"] == "final_price"]
    assert final_conflicts == []
    assert CONFLICT_FLAG in flags  # 模块 total 修正/保留的冲突明细
    # LLM 原 summary 存档备查
    assert offer["_derived"]["summary_llm_original"]["untaxed_total"] == 12.06


def test_gold_offer2_shared_cell_dedupe_and_summary():
    offer = _offer(1)
    flags = derive_offer(offer, ir=_ir())
    up = offer["unit_price"]

    # 第二行 raw_text 为裸金额（"0.30"，单据该行只有数字单元格）：不据此判不准，
    # 仍按语义 keeper 去重——全检保留 0.30，镭雕副本置零
    assert _item(offer, "inspection", "全检")["amount_per_pc"] == 0.3
    assert _item(offer, "processing", "镭雕")["amount_per_pc"] == 0.0
    assert _item(offer, "packaging_transport", "运输")["amount_per_pc"] == 0.0
    assert up["processing"]["total"] == 6.35  # 1.2+1.85+0.9+0.5+1.5+0.4（镭雕副本已置零）
    assert up["packaging_transport"]["total"] == 0.10

    assert up["summary"]["untaxed_total"] == pytest.approx(12.68)
    assert up["summary"]["tax_amount"] == pytest.approx(1.65, abs=0.01)
    assert up["summary"]["taxed_total"] == pytest.approx(14.33, abs=0.01)
    assert up["summary"]["final_unit_price_taxed"] == 14.33
    assert [_c for _c in offer["_derived"]["conflicts"] if _c["kind"] == "final_price"] == []
    assert SHARED_CELL_FLAG in flags
    assert CONFLICT_FLAG in flags


def test_gold_amount_strings_match_two_decimal_text():
    """出处比对为词边界口径：0.3 命中 raw_text "0.30"，但不误中 "10.30"。"""
    assert amount_in_text("全检 0.30", 0.3)
    assert not amount_in_text("合计 10.30", 0.3)
    assert not amount_in_text("税率 13.0%", 0.0)  # "0" 不再误中 "13.0"


# ---------------------------------------------------------------------------
# 规则单测
# ---------------------------------------------------------------------------

def _mini_offer(processing_total=4.0, processing_items=None, sga_items=None,
                inspection_total=None, summary=None) -> dict:
    return {
        "unit_price": {
            "materials": {"total": 4.0, "items": [{"name": "铝材", "amount_per_pc": 4.0}]},
            "processing": {"total": processing_total, "items": processing_items or []},
            "inspection": {"total": inspection_total, "items": []},
            "packaging_transport": {"total": None, "items": []},
            "sga_tax": {"total": None, "items": sga_items or []},
            "other": {"total": None, "items": []},
            "summary": summary or {},
        }
    }


def test_shared_cell_dedupe_keeper_semantic_priority():
    """同 location 同金额且判为真共享 → keeper 语义优先：检验类科目（全检）在检验费模块
    优先保留，加工侧误植副本置 0；Σitems/未税按去重后口径。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "镭雕", "amount_per_pc": 0.3,
             "evidence": {"location": "p1!R1", "raw_text": "镭雕 0.30"}},
        ],
        inspection_total=None,
        sga_items=[{"name": "管理费", "item_type": "管理费", "amount_per_pc": 1.0}],
    )
    offer["unit_price"]["inspection"]["items"] = [
        {"name": "全检", "amount_per_pc": 0.3,
         "evidence": {"location": "p1!R1", "raw_text": "全检 0.30"}},
    ]
    offer["unit_price"]["inspection"]["total"] = None
    flags = derive_offer(offer)
    assert _item(offer, "processing", "镭雕")["amount_per_pc"] == 0.0
    assert "与『全检』共享单元格 p1!R1" in (_item(offer, "processing", "镭雕")["note"] or "")
    assert _item(offer, "inspection", "全检")["amount_per_pc"] == 0.3
    assert SHARED_CELL_FLAG in flags
    assert CALC_ABNORMAL_FLAG not in flags
    # 未税 = 材料4.0 + 加工0 + 检验0.3 + 管理1.0
    assert offer["unit_price"]["summary"]["untaxed_total"] == pytest.approx(5.3)
    assert offer["unit_price"]["inspection"]["total"] == 0.3  # null 回填去重后 Σitems


def test_shared_cell_dedupe_module_order_fallback():
    """真共享且无检验类科目（包装/运输 同模块）→ 回退模块顺序+条目索引，保留第一条。"""
    offer = _mini_offer(processing_total=None)
    offer["unit_price"]["packaging_transport"]["items"] = [
        {"name": "包装", "amount_per_pc": 0.1, "item_type": "包装",
         "evidence": {"location": "p1!R2", "raw_text": "包装 0.10"}},
        {"name": "运输", "amount_per_pc": 0.1, "item_type": "运输",
         "evidence": {"location": "p1!R2", "raw_text": "运输 0.10"}},
    ]
    flags = derive_offer(offer)
    assert _item(offer, "packaging_transport", "包装")["amount_per_pc"] == 0.1
    assert _item(offer, "packaging_transport", "运输")["amount_per_pc"] == 0.0
    assert SHARED_CELL_FLAG in flags
    # 未税 = 材料4.0 + 包装运输0.1
    assert offer["unit_price"]["summary"]["untaxed_total"] == pytest.approx(4.1)


def test_shared_cell_ambiguous_header_fragment_not_zeroed():
    """表头碎片特征（"镭雕"是"镭雕破氧白"的片段，换行表头拆出的伪科目）→ 判不准：
    两条目都保留原值不置零，打 calc_abnormal + shared_cell_ambiguous 冲突交人工。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "镭雕", "amount_per_pc": 0.4,
             "evidence": {"location": "p1!R3", "raw_text": "镭雕 0.40"}},
            {"name": "镭雕破氧白", "amount_per_pc": 0.4,
             "evidence": {"location": "p1!R3", "raw_text": "镭雕破氧白 0.40"}},
        ],
    )
    flags = derive_offer(offer)
    assert _item(offer, "processing", "镭雕")["amount_per_pc"] == 0.4
    assert _item(offer, "processing", "镭雕破氧白")["amount_per_pc"] == 0.4
    assert SHARED_CELL_FLAG not in flags
    assert CALC_ABNORMAL_FLAG in flags
    conflict = next(c for c in offer["_derived"]["conflicts"] if c["kind"] == "shared_cell_ambiguous")
    assert "p1!R3" in conflict["detail"] and "人工" in conflict["detail"]


def test_shared_cell_ambiguous_name_not_in_raw_not_zeroed():
    """科目名未真实出现在带文本的 raw_text（"镭雕"的出处原文写的是"全检 0.30"——破氧白/全检
    张冠李戴场景）→ 判不准：都不置零，打 calc_abnormal，宁可重复也不静默丢钱。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "镭雕", "amount_per_pc": 0.3,
             "evidence": {"location": "p1!R4", "raw_text": "全检 0.30"}},
        ],
    )
    offer["unit_price"]["inspection"]["items"] = [
        {"name": "全检", "amount_per_pc": 0.3,
         "evidence": {"location": "p1!R4", "raw_text": "全检 0.30"}},
    ]
    flags = derive_offer(offer)
    assert _item(offer, "processing", "镭雕")["amount_per_pc"] == 0.3
    assert _item(offer, "inspection", "全检")["amount_per_pc"] == 0.3
    assert SHARED_CELL_FLAG not in flags
    assert CALC_ABNORMAL_FLAG in flags
    assert any(c["kind"] == "shared_cell_ambiguous" for c in offer["_derived"]["conflicts"])


def test_shared_cell_not_deduped_when_amounts_differ():
    """同 location 但金额不同 → 不去重（激光熔覆0.20/全尺寸检验0.30 场景）。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "激光熔覆", "amount_per_pc": 0.2,
             "evidence": {"location": "p1!R1", "raw_text": "激光熔覆 0.20"}},
        ],
    )
    offer["unit_price"]["inspection"]["items"] = [
        {"name": "全尺寸检验", "amount_per_pc": 0.3,
         "evidence": {"location": "p1!R1", "raw_text": "全尺寸检验 0.30"}},
    ]
    flags = derive_offer(offer)
    assert _item(offer, "processing", "激光熔覆")["amount_per_pc"] == 0.2
    assert _item(offer, "inspection", "全尺寸检验")["amount_per_pc"] == 0.3
    assert SHARED_CELL_FLAG not in flags


def test_shared_cell_not_deduped_without_evidence_match():
    """金额文本未在每个条目 raw_text 中出现 → 判据不足，不去重。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "A", "amount_per_pc": 1.0,
             "evidence": {"location": "p1!R1", "raw_text": "A 1.00"}},
            {"name": "B", "amount_per_pc": 1.0,
             "evidence": {"location": "p1!R1", "raw_text": "B 打包"}},
        ],
    )
    flags = derive_offer(offer)
    assert _item(offer, "processing", "A")["amount_per_pc"] == 1.0
    assert _item(offer, "processing", "B")["amount_per_pc"] == 1.0
    assert SHARED_CELL_FLAG not in flags


def test_tax_amount_conflict_kept_with_flag():
    """有出处的税费金额与 税率×未税 不符 → 保留单据值 + cross_validation_conflict。"""
    offer = _mini_offer(
        sga_items=[{"name": "增值税", "item_type": "税费", "amount_per_pc": 2.0, "rate": 0.13,
                    "evidence": {"location": "p1!R9", "raw_text": "增值税 2.00"}}]
    )
    flags = derive_offer(offer)
    # 未税=材料4.0+加工4.0（加工无明细回退 total）=8.0，0.13×8.0=1.04，与 2.0 不符 → 保留 2.0
    assert offer["unit_price"]["summary"]["tax_amount"] == 2.0
    assert offer["unit_price"]["summary"]["untaxed_total"] == pytest.approx(8.0)
    assert offer["unit_price"]["summary"]["taxed_total"] == pytest.approx(10.0)
    assert CONFLICT_FLAG in flags
    conflict = next(c for c in offer["_derived"]["conflicts"] if c["kind"] == "tax_amount")
    assert conflict["document_value"] == 2.0
    assert conflict["derived_value"] == pytest.approx(1.04)


def test_tax_hallucination_derived_from_rate():
    """税费金额无出处（raw_text 中无该金额）→ 视为缺失，按 税率×未税 派生。"""
    offer = _mini_offer(
        sga_items=[{"name": "税费", "item_type": "税费", "amount_per_pc": 2.0, "rate": 0.13,
                    "evidence": {"location": "p1!R9", "raw_text": "税率 13%"}}]
    )
    flags = derive_offer(offer)
    tax_item = offer["unit_price"]["sga_tax"]["items"][0]
    assert tax_item["amount_per_pc"] == pytest.approx(0.13 * 8.0)
    assert "原抽取值 2.0 与出处不符，按税率×未税派生" == tax_item["note"]
    assert offer["_derived"]["tax_derived"] is True
    # 未税8.0 + 税额1.04
    assert offer["unit_price"]["summary"]["taxed_total"] == pytest.approx(9.04)
    assert not any(c["kind"] == "tax_amount" for c in offer["_derived"]["conflicts"])


def test_tax_missing_amount_derived_with_note():
    """税费 amount 为 null 且有 rate → 派生，note 含"派生值：税率×未税"。"""
    offer = _mini_offer(
        sga_items=[{"name": "税费", "item_type": "税费", "amount_per_pc": None, "rate": 0.13,
                    "note": "税率13%"}]
    )
    derive_offer(offer)
    tax_item = offer["unit_price"]["sga_tax"]["items"][0]
    assert tax_item["amount_per_pc"] == pytest.approx(1.04)
    assert DERIVED_TAX_NOTE in (tax_item["note"] or "")
    assert "税率13%" in tax_item["note"]


def test_tax_zero_amount_treated_as_missing_and_derived():
    """税费 amount=0 且 rate 非空 → 业务上无零税额单据，一律视为缺失按 税率×未税 派生
    （词边界前 "0" 会误中 raw_text『税率 13.0%』而保留 0.0 的真实 bug）。"""
    offer = _mini_offer(
        sga_items=[{"name": "税费", "item_type": "税费", "amount_per_pc": 0.0, "rate": 0.13,
                    "evidence": {"location": "p1!R9", "raw_text": "税率 13.0%"}}]
    )
    flags = derive_offer(offer)
    tax_item = offer["unit_price"]["sga_tax"]["items"][0]
    assert tax_item["amount_per_pc"] == pytest.approx(0.13 * 8.0)
    assert "视为税额缺失" in (tax_item["note"] or "")
    assert offer["_derived"]["tax_derived"] is True
    assert offer["unit_price"]["summary"]["tax_amount"] == pytest.approx(1.04)
    assert offer["unit_price"]["summary"]["taxed_total"] == pytest.approx(9.04)
    assert not any(c["kind"] == "tax_amount" for c in offer["_derived"]["conflicts"])


def test_tax_zero_amount_without_rate_kept_with_conflict():
    """税费 amount=0 且 rate 也为空 → 无法派生，保留原值 + cross_validation_conflict。"""
    offer = _mini_offer(
        sga_items=[{"name": "税费", "item_type": "税费", "amount_per_pc": 0.0,
                    "evidence": {"location": "p1!R9", "raw_text": "税"}}]
    )
    flags = derive_offer(offer)
    tax_item = offer["unit_price"]["sga_tax"]["items"][0]
    assert tax_item["amount_per_pc"] == 0.0
    assert offer["unit_price"]["summary"]["tax_amount"] == 0.0
    assert CONFLICT_FLAG in flags
    conflict = next(c for c in offer["_derived"]["conflicts"] if c["kind"] == "tax_amount")
    assert conflict["document_value"] == 0.0


def test_module_total_conflict_with_ir_provenance_kept():
    """规则 B：total 与 Σitems 冲突，IR 中存在该数 → 单据印有合计，保留 + flag。"""
    offer = _mini_offer(
        processing_total=5.0,
        processing_items=[{"name": "CNC", "amount_per_pc": 1.0}, {"name": "氧化", "amount_per_pc": 2.0}],
    )
    ir = {"tables": [{"sheet": "p1", "cells": [{"row": 1, "col": 9, "value": "5.00"}]}]}
    flags = derive_offer(offer, ir=ir)
    assert offer["unit_price"]["processing"]["total"] == 5.0
    assert "processing" not in offer["_derived"]["corrected_module_totals"]
    conflict = next(c for c in offer["_derived"]["conflicts"] if c["kind"] == "module_total")
    assert "保留单据值" in conflict["detail"]
    assert CONFLICT_FLAG in flags


def test_module_total_conflict_without_ir_provenance_corrected():
    """规则 B：IR 中无该数 → LLM 推算值，total 改为 Σitems（去重后）并记录。"""
    offer = _mini_offer(
        processing_total=5.45,
        processing_items=[{"name": "CNC", "amount_per_pc": 1.95}, {"name": "氧化", "amount_per_pc": 5.0}],
    )
    ir = {"tables": [{"sheet": "p1", "cells": [{"row": 1, "col": 9, "value": "6.95"}]}]}
    flags = derive_offer(offer, ir=ir)
    assert offer["unit_price"]["processing"]["total"] == 6.95
    assert offer["_derived"]["corrected_module_totals"]["processing"] == {
        "original_total": 5.45,
        "items_sum": 6.95,
    }
    conflict = next(c for c in offer["_derived"]["conflicts"] if c["kind"] == "module_total")
    assert "无出处" in conflict["detail"]
    assert CONFLICT_FLAG in flags


def test_module_total_conflict_without_ir_kept():
    """规则 B 保守路径：ir=None → 保留单据 total + flag（维持原行为）。"""
    offer = _mini_offer(
        processing_total=5.0,
        processing_items=[{"name": "CNC", "amount_per_pc": 1.0}, {"name": "氧化", "amount_per_pc": 2.0}],
    )
    flags = derive_offer(offer)
    assert offer["unit_price"]["processing"]["total"] == 5.0
    assert "processing" not in offer["_derived"]["corrected_module_totals"]
    assert flags == [CONFLICT_FLAG]


def test_module_total_null_filled_with_items_sum_including_bundle():
    """total 为 null → 回填 Σitems；bundle_flag=true 行整行计入，不拆分。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "CNC", "amount_per_pc": 1.0},
            {"name": "喷砂/氧化", "amount_per_pc": 1.5, "bundle_flag": True,
             "bundle_members": ["AT-ZP-005", "AT-ZH-013"]},
        ],
    )
    flags = derive_offer(offer)
    assert offer["unit_price"]["processing"]["total"] == 2.5
    assert "processing" in offer["_derived"]["filled_module_totals"]
    assert flags == []
    assert offer["unit_price"]["summary"]["untaxed_total"] == pytest.approx(6.5)


def test_derive_idempotent():
    """重复调用（含 ir）：重算值、flags、归档均一致（基于自身输出仍是同一值）。"""
    offer = _offer(0)
    derive_offer(offer, ir=_ir())
    snapshot = copy.deepcopy(offer)
    flags_second = derive_offer(offer, ir=_ir())
    assert flags_second == [SHARED_CELL_FLAG, CONFLICT_FLAG]
    assert offer == snapshot


def test_derive_envelope_returns_flags_per_offer():
    envelope = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = derive_envelope(envelope, ir=_ir())
    assert result["envelope"] is envelope
    assert len(result["flags_per_offer"]) == 2
    assert all(SHARED_CELL_FLAG in flags for flags in result["flags_per_offer"])
    assert all(CONFLICT_FLAG in flags for flags in result["flags_per_offer"])
