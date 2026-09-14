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
from app.persist import collect_flags

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


# ---------------------------------------------------------------------------
# 金额未印出（null）：合计只能算下限，不得静默当完整合计
# ---------------------------------------------------------------------------

def test_missing_amount_records_conflict_and_flag():
    """材料费格未印出（null）→ 记 amount_missing 冲突，合计标注为下限，并打 calc_abnormal。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "CNC", "amount_per_pc": 1.5},
            {"name": "阳极", "amount_per_pc": None, "note": "单据未印出金额"},
        ],
    )
    flags = derive_offer(offer)
    assert CONFLICT_FLAG in flags
    assert "calc_abnormal" in collect_flags(offer, "ok")  # persist 侧据冲突补打标
    conflicts = [c for c in offer["_derived"]["conflicts"] if c["kind"] == "amount_missing"]
    assert len(conflicts) == 1
    assert conflicts[0]["module"] == "processing"
    assert conflicts[0]["derived_value"] == 1.5
    assert "1 项明细金额单据未印出" in conflicts[0]["detail"]
    assert offer["_derived"]["modules_with_missing_amounts"] == {"processing": 1}


def test_missing_amount_conflict_replayed_idempotently():
    """双跑（persist 路径不传 ir）冲突记录一致，不重复累积。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[{"name": "CNC", "amount_per_pc": None}],
    )
    derive_offer(offer)
    first = [c for c in offer["_derived"]["conflicts"] if c["kind"] == "amount_missing"]
    derive_offer(offer)
    second = [c for c in offer["_derived"]["conflicts"] if c["kind"] == "amount_missing"]
    assert first == second and len(second) == 1


def test_tax_null_amount_is_not_flagged_as_missing():
    """税费条目为 null 是契约要求（脚本按税率派生），不算「金额未印出」。"""
    offer = _mini_offer(
        sga_items=[
            {"name": "增值税", "item_type": "税费", "amount_per_pc": None, "rate": 0.13},
            {"name": "管理费", "item_type": "管理费", "amount_per_pc": 1.0},
        ],
    )
    derive_offer(offer)
    assert "sga_tax" not in offer["_derived"].get("modules_with_missing_amounts", {})
    assert not [c for c in offer["_derived"]["conflicts"] if c["kind"] == "amount_missing"]


def test_module_total_all_null_items_stays_null():
    """材料费明细金额全部未印出（null）→ 模块 total 保持 null（不以 0 占位），
    只记 amount_missing 冲突并标明「无可信合计」。"""
    offer = _mini_offer(processing_total=None)
    offer["unit_price"]["materials"] = {
        "total": None,
        "items": [{"name": "材料费", "amount_per_pc": None, "note": "料重 28；料价 0.96"}],
    }
    derive_offer(offer)
    assert offer["unit_price"]["materials"]["total"] is None
    assert "materials" not in offer["_derived"].get("filled_module_totals", [])
    assert offer["_derived"]["modules_with_missing_amounts"] == {"materials": 1}
    conflict = next(c for c in offer["_derived"]["conflicts"] if c["kind"] == "amount_missing")
    assert conflict["module"] == "materials"
    assert conflict["derived_value"] is None
    assert "无可信合计" in conflict["detail"]


def test_module_total_partial_null_items_filled_with_lower_bound():
    """部分明细金额未印出 → 仍按已知项之和回填下限（与全 null 区分）。"""
    offer = _mini_offer(
        processing_total=None,
        processing_items=[
            {"name": "CNC", "amount_per_pc": 1.5},
            {"name": "阳极", "amount_per_pc": None},
        ],
    )
    derive_offer(offer)
    assert offer["unit_price"]["processing"]["total"] == 1.5
    assert "processing" in offer["_derived"]["filled_module_totals"]


# ---------------------------------------------------------------------------
# 规则 D：材料栏只印「料价」时的材料费兜底（勾稽背书）
#
# 真实缺陷（博业模具，quote 1）：报价单材料栏只有「料重 28 / 料价 0.96」两列，
# 无材料费金额列。prompt 契约要求「金额一律照抄，不印金额填 null」，LLM 照办 →
# 材料费 null → 未税少 0.96 → 含税 18.645 vs 单据 18.6 超出 0.05（实际 15.54+
# 0.96=16.5，Δ 0.045 未超），但模块 total=null 触发 amount_missing → calc_abnormal
# + cross_validation_conflict。规则 D 在有勾稽背书时把料价照抄为每件材料费。
# ---------------------------------------------------------------------------

def _material_offer(materials=None, sga_items=None, final=18.6) -> dict:
    """复刻博业报价单结构：加工 4.0+3.5+1.9+1.8+1.5+0.2=12.9，检验 0.35，
    包装运输 0.2，损耗/管理/毛利 1.2+0.1+0.79=2.09，税点 13% 金额未印出。"""
    return {
        "unit_price": {
            "materials": {
                "total": None,
                "items": materials
                if materials is not None
                else [{
                    "name": "铝合金",
                    "amount_per_pc": None,
                    "note": "料重 28；料价 0.96",
                    "evidence": {"location": "page_1!R3C9:R3C10", "raw_text": "28 / 0.96"},
                }],
            },
            "processing": {
                "total": None,
                "items": [
                    {"name": "啤工", "amount_per_pc": 4.0},
                    {"name": "CNC", "amount_per_pc": 3.5},
                    {"name": "抛光", "amount_per_pc": 1.9},
                    {"name": "氧化", "amount_per_pc": 1.8},
                    {"name": "喷砂", "amount_per_pc": 1.5},
                    {"name": "镭雕", "amount_per_pc": 0.2},
                ],
            },
            "inspection": {"total": None, "items": [{"name": "全检", "amount_per_pc": 0.35}]},
            "packaging_transport": {"total": None, "items": [{"name": "包装", "amount_per_pc": 0.2}]},
            "sga_tax": {
                "total": None,
                "items": sga_items
                if sga_items is not None
                else [
                    {"name": "损耗", "item_type": "损耗", "amount_per_pc": 1.2},
                    {"name": "管理", "item_type": "管理费", "amount_per_pc": 0.1},
                    {"name": "毛利", "item_type": "利润", "amount_per_pc": 0.79},
                    {"name": "税点13%", "item_type": "税费", "amount_per_pc": None, "rate": 0.13,
                     "evidence": {"location": "page_1!R3C28", "raw_text": "2.14"}},
                ],
            },
            "other": {"total": None, "items": []},
            "summary": {"final_unit_price_taxed": final},
        }
    }


def test_material_price_corroborated_fills_material_fee():
    """料价 0.96 计入后含税 16.5×1.13=18.645，与单据 18.6 差 0.045 ≤ 0.05 → 采纳为材料费。"""
    offer = _material_offer()
    derive_offer(offer)
    item = offer["unit_price"]["materials"]["items"][0]
    assert item["amount_per_pc"] == 0.96
    assert item["note"].startswith("料重 28；料价 0.96；")
    assert "派生值：料价照抄为每件材料费（勾稽背书通过）" in item["note"]
    # 模块 total 由规则 B 回填，未税/税费/summary 随之修正；final 保留单据值
    assert offer["unit_price"]["materials"]["total"] == 0.96
    summary = offer["unit_price"]["summary"]
    assert (summary["untaxed_total"], summary["tax_amount"], summary["taxed_total"]) == (16.5, 2.145, 18.645)
    assert summary["final_unit_price_taxed"] == 18.6
    # 金额已补齐：不再有 amount_missing / 无背书冲突，也不算勾稽异常
    assert offer["_derived"]["modules_with_missing_amounts"] == {}
    assert offer["_derived"]["conflicts"] == []
    assert "calc_abnormal" not in collect_flags(offer, "pass")
    assert "cross_validation_conflict" not in collect_flags(offer, "pass")
    # 归档留痕（可追溯 LLM 原值 vs 派生值）
    fallback = offer["_derived"]["material_price_fallback"]
    assert len(fallback) == 1
    assert (fallback[0]["value"], fallback[0]["items"]) == (0.96, 1)
    assert (fallback[0]["document_final_unit_price_taxed"], fallback[0]["derived_taxed_total"]) == (18.6, 18.645)
    assert offer["_derived"]["summary_llm_original"]["final_unit_price_taxed"] == 18.6


def test_material_price_uncorroborated_keeps_null():
    """勾稽不背书（含税单价对不上）→ 不填，保持 null 并记无背书冲突，仍打 calc_abnormal。"""
    offer = _material_offer(final=18.7)  # Δ = 18.645 - 18.7 = -0.055 > 0.05
    derive_offer(offer)
    item = offer["unit_price"]["materials"]["items"][0]
    assert item["amount_per_pc"] is None
    assert item["note"] == "料重 28；料价 0.96"
    assert offer["unit_price"]["materials"]["total"] is None
    uncorroborated = [c for c in offer["_derived"]["conflicts"] if c["kind"] == "material_price_uncorroborated"]
    assert len(uncorroborated) == 1
    assert uncorroborated[0]["document_value"] == 0.96
    assert uncorroborated[0]["derived_value"] is None
    assert "勾稽不通过" in uncorroborated[0]["detail"]
    assert offer["_derived"]["modules_with_missing_amounts"] == {"materials": 1}
    assert "calc_abnormal" in collect_flags(offer, "fail")


def test_material_price_missing_anchor_keeps_null():
    """缺单据含税单价（无法勾稽）→ 不填，冲突说明缺背书依据。"""
    offer = _material_offer(final=None)
    derive_offer(offer)
    assert offer["unit_price"]["materials"]["items"][0]["amount_per_pc"] is None
    conflict = [c for c in offer["_derived"]["conflicts"] if c["kind"] == "material_price_uncorroborated"]
    assert len(conflict) == 1
    assert "无法勾稽背书" in conflict[0]["detail"]


def test_material_price_without_tax_item_keeps_null():
    """无税费条目（无法估算税额）→ 不填，同样记无背书冲突。"""
    offer = _material_offer(
        sga_items=[{"name": "损耗", "item_type": "损耗", "amount_per_pc": 1.2}],
    )
    derive_offer(offer)
    assert offer["unit_price"]["materials"]["items"][0]["amount_per_pc"] is None
    conflict = [c for c in offer["_derived"]["conflicts"] if c["kind"] == "material_price_uncorroborated"]
    assert len(conflict) == 1
    assert "无法勾稽背书" in conflict[0]["detail"]


def test_material_price_tolerance_boundary():
    """允差取闭区间：Δ 恰为 0.05 采纳，超出 0.05 不采纳。"""
    at_boundary = _material_offer(final=18.595)
    derive_offer(at_boundary)
    assert at_boundary["unit_price"]["materials"]["items"][0]["amount_per_pc"] == 0.96

    beyond = _material_offer(final=18.59)
    derive_offer(beyond)
    assert beyond["unit_price"]["materials"]["items"][0]["amount_per_pc"] is None


def test_material_price_candidate_falls_back_to_two_number_evidence():
    """note 只印料重（无「料价」关键词）时，退一步用 evidence 的两个数字取非料重项。"""
    offer = _material_offer(
        materials=[{
            "name": "铝合金",
            "amount_per_pc": None,
            "note": "料重 28",
            "evidence": {"location": "page_1!R3C9:R3C10", "raw_text": "28 / 0.96"},
        }]
    )
    derive_offer(offer)
    assert offer["unit_price"]["materials"]["items"][0]["amount_per_pc"] == 0.96


def test_material_price_single_number_evidence_no_candidate():
    """evidence 只有一个数（就是料重）→ 不猜，保持 null 且不新增无背书冲突。"""
    offer = _material_offer(
        materials=[{
            "name": "铝合金",
            "amount_per_pc": None,
            "note": "料重 28",
            "evidence": {"location": "page_1!R3C9", "raw_text": "28"},
        }]
    )
    derive_offer(offer)
    assert offer["unit_price"]["materials"]["items"][0]["amount_per_pc"] is None
    assert not [c for c in offer["_derived"]["conflicts"] if c["kind"] == "material_price_uncorroborated"]
    assert "materials" in offer["_derived"]["modules_with_missing_amounts"]


def test_material_price_multiple_null_items_summed_for_gate():
    """多个材料行都只印料价 → 以候选之和参与勾稽（0.5 + 0.46 = 0.96）。"""
    offer = _material_offer(
        materials=[
            {"name": "铝合金", "amount_per_pc": None, "note": "料价 0.5"},
            {"name": "锌合金", "amount_per_pc": None, "note": "料价 0.46"},
        ]
    )
    derive_offer(offer)
    items = offer["unit_price"]["materials"]["items"]
    assert [i["amount_per_pc"] for i in items] == [0.5, 0.46]
    assert offer["unit_price"]["materials"]["total"] == 0.96
    assert offer["_derived"]["material_price_fallback"][0]["value"] == 0.96
    assert offer["_derived"]["material_price_fallback"][0]["items"] == 2


def test_material_price_partial_candidates_still_gated_on_sum():
    """部分行有候选、部分行没有 → 只按有候选的量级勾稽（无候选行保持 null，
    仍然记 amount_missing，避免把不完整合计当完整）。"""
    offer = _material_offer(
        materials=[
            {"name": "铝合金", "amount_per_pc": None, "note": "料价 0.96"},
            {"name": "锌合金", "amount_per_pc": None, "note": "未印出金额"},
        ]
    )
    derive_offer(offer)
    items = offer["unit_price"]["materials"]["items"]
    assert items[0]["amount_per_pc"] == 0.96
    assert items[1]["amount_per_pc"] is None
    assert offer["_derived"]["modules_with_missing_amounts"] == {"materials": 1}


def test_material_price_rule_skips_when_total_printed():
    """单据印有材料费合计 → 交规则 B 处理，规则 D 不介入（避免重复计材料费）。"""
    offer = _material_offer()
    offer["unit_price"]["materials"]["total"] = 0.96
    derive_offer(offer)
    item = offer["unit_price"]["materials"]["items"][0]
    assert item["amount_per_pc"] is None
    assert "material_price_fallback" not in offer["_derived"]
    assert not [c for c in offer["_derived"]["conflicts"] if c["kind"] == "material_price_uncorroborated"]


def test_material_price_rule_idempotent():
    """双跑（persist 路径不传 ir）结果一致：金额不重复累加，归档与冲突不累积。"""
    offer = _material_offer()
    derive_offer(offer)
    first = copy.deepcopy(offer)
    derive_offer(offer)
    assert offer == first
    assert len(offer["_derived"]["material_price_fallback"]) == 1
    assert offer["_derived"]["conflicts"] == []

    reject = _material_offer(final=18.7)
    derive_offer(reject)
    snapshot = copy.deepcopy(reject)
    derive_offer(reject)
    assert reject == snapshot
    assert len([c for c in reject["_derived"]["conflicts"] if c["kind"] == "material_price_uncorroborated"]) == 1


def _moq_offer(moq=None, other_info=None, notes=None, tooling_note=None) -> dict:
    """规则 E 用最小 offer：basic.moq 可空，其它信息/条目备注按需注入。"""
    offer = _material_offer()
    offer["basic"] = {"currency": "CNY", "moq": moq}
    offer["other_info"] = other_info
    if notes:
        for item, note in zip(offer["unit_price"]["processing"]["items"], notes):
            item["note"] = note
    if tooling_note:
        offer["tooling"] = {"total": None, "molds": {"total": None, "items": [
            {"name": "模具", "amount": 43000, "note": tooling_note}]}}
    return offer


def test_moq_fallback_from_other_info_threshold_clause():
    """豪泽/美格式写法：LLM 漏抽起订量，脚本从「其它信息」商务条款兜底识别并留痕。"""
    offer = _moq_offer(other_info="## 商务条款\n- 订单量少于2000PCS加收开机费1000元。\n")
    derive_offer(offer)
    assert offer["basic"]["moq"] == 2000
    fallback = offer["_derived"]["moq_fallback"]
    assert (fallback["value"], fallback["snippet"]) == (2000, "订单量少于2000")
    assert fallback["note"] == "派生值：由「其它信息」/备注文本识别起订量"
    assert offer["_derived"]["conflicts"] == []


def test_moq_fallback_from_item_note_and_tooling_note():
    """其它信息缺失时看条目备注；其它信息无起订量时看模治具备注。"""
    from_note = _moq_offer(notes=["MOQ：3K", None, None, None, None, None])
    derive_offer(from_note)
    assert from_note["basic"]["moq"] == 3000

    from_tooling = _moq_offer(other_info="## 交期\n- 量产 25 天\n", tooling_note="模具全预付；MOQ 5K")
    derive_offer(from_tooling)
    assert from_tooling["basic"]["moq"] == 5000


def test_moq_llm_value_wins_and_absent_stays_null():
    """LLM 已给出起订量时不覆盖；全篇没有起订量表述（弗我式）保持 null，不猜测。"""
    declared = _moq_offer(moq=5000, other_info="- 订单量少于2000PCS加收开机费1000元。\n")
    derive_offer(declared)
    assert declared["basic"]["moq"] == 5000
    assert "moq_fallback" not in declared["_derived"]

    absent = _moq_offer(other_info="## 商务条款\n- 报价有效期 15 天\n- 数量(PCS)：1\n")
    derive_offer(absent)
    assert absent["basic"]["moq"] is None
    assert "moq_fallback" not in absent["_derived"]


def test_moq_rule_idempotent():
    """双跑（persist 无 IR 重放）结果一致：归档只写一次。"""
    offer = _moq_offer(other_info="- 订单量少于3000PCS加收开机费800元。\n")
    derive_offer(offer)
    first = copy.deepcopy(offer)
    derive_offer(offer)
    assert offer == first
    assert offer["_derived"]["moq_fallback"]["value"] == 3000


def _moq_options_offer(moq=None, moq_options=None, other_info=None) -> dict:
    """moq_options 相关用例：LLM 输出（可带脏数据）与「其它信息」文本按需注入。"""
    offer = _material_offer()
    offer["basic"] = {"currency": "CNY", "moq": moq, "moq_options": moq_options}
    offer["other_info"] = other_info
    return offer


def test_moq_options_kept_and_primary_backfilled():
    """LLM 给出分档但漏了主起订量：保留分档，并用通用（不限条件）档回填 basic.moq。"""
    offer = _moq_options_offer(
        moq_options=[
            {"condition": "定制皮革单色", "value": 40000, "note": "金属管需提供3%损耗"},
            {"condition": "皮革现货单色", "value": 3000, "note": None},
        ]
    )
    derive_offer(offer)
    assert [(o["condition"], o["value"]) for o in offer["basic"]["moq_options"]] == [
        ("定制皮革单色", 40000),
        ("皮革现货单色", 3000),
    ]
    # 没有「不限条件」档时取首档（LLM 输出顺序 = 原文出现顺序）
    assert offer["basic"]["moq"] == 40000
    assert offer["_derived"]["moq_primary_from_options"] == {
        "value": 40000,
        "condition": "定制皮革单色",
        "note": "派生值：由起订量分档取通用（不限条件）档",
    }


def test_moq_options_cleaned_and_degraded_to_single_moq():
    """脏数据清理：无数值/重复项丢弃、数值字符串转整数；只剩一条且不带条件时视为普通起订量（分档置空）。"""
    offer = _moq_options_offer(
        moq=None,
        moq_options=[
            {"condition": "皮革现货单色", "value": 3000},
            {"condition": "皮革现货单色", "value": 3000},
            {"condition": "定制皮革单色", "value": "40000"},
            {"condition": None, "value": 0},
            "不是对象",
        ],
    )
    derive_offer(offer)
    assert offer["basic"]["moq_options"] == [
        {"condition": "皮革现货单色", "value": 3000, "note": None},
        {"condition": "定制皮革单色", "value": 40000, "note": None},
    ]
    assert offer["basic"]["moq"] == 3000

    single = _moq_options_offer(moq_options=[{"condition": None, "value": 2000}])
    derive_offer(single)
    assert single["basic"]["moq_options"] is None
    assert single["basic"]["moq"] == 2000  # 单档数值先回填主起订量再降级
    assert single["_derived"]["moq_primary_from_options"]["value"] == 2000


def test_moq_options_fallback_from_other_info():
    """LLM 未给分档时，脚本从「其它信息」识别多档并留痕；顺带兜底主起订量。"""
    offer = _moq_options_offer(
        other_info="## 商务条款\n- 金属管需要提供3%损耗\n- 皮革现货单色 MOQ：3K\n- 定制皮革单色 MOQ：40K\n"
    )
    derive_offer(offer)
    assert offer["basic"]["moq"] == 3000
    assert [(o["condition"], o["value"]) for o in offer["basic"]["moq_options"]] == [
        ("皮革现货单色", 3000),
        ("定制皮革单色", 40000),
    ]
    assert offer["_derived"]["moq_options_fallback"]["note"] == "派生值：由「其它信息」/备注文本识别起订量"
    assert offer["_derived"]["moq_options_fallback"]["count"] == 2


def test_moq_options_absent_stays_null():
    """全篇没有分档表述时保持 null；单档普通起订量也不立分档。"""
    offer = _moq_options_offer(other_info="## 商务条款\n- 订单量少于2000PCS加收开机费1000元。\n")
    derive_offer(offer)
    assert offer["basic"]["moq"] == 2000
    assert offer["basic"]["moq_options"] is None
    assert "moq_options_fallback" not in offer["_derived"]


def test_moq_options_rule_idempotent():
    """双跑一致：分档与回填都不重复写。"""
    offer = _moq_options_offer(
        other_info="- 起订量：单色3K，双色5K\n",
        moq_options=[{"condition": "单色", "value": 3000}, {"condition": "双色", "value": 5000}],
    )
    derive_offer(offer)
    first = copy.deepcopy(offer)
    derive_offer(offer)
    assert offer == first
