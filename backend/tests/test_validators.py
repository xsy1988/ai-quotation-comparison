"""分层校验器框架：L0 结构 / L1 溯源 / L2 勾稽 / run_validators 短路。"""

import json
from pathlib import Path

import pytest

from app.ir import IR
from app.validate.validators import (
    Issue,
    parse_location,
    run_validators,
    validate_l0_schema,
    validate_l1_traceability,
    validate_l2_reconcile,
)

FIXTURES = Path(__file__).parent / "fixtures"
ALL_MODULES = ("materials", "processing", "inspection", "packaging_transport", "sga_tax", "other")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _ir() -> IR:
    return IR.from_dict(load_fixture("ir_chuangfeng.json"))


def _item(amount, raw_text, location="page_1!R4C9", item_type=None, name="测试项") -> dict:
    item = {"name": name, "amount_per_pc": amount,
            "evidence": {"location": location, "raw_text": raw_text}}
    if item_type is not None:
        item["item_type"] = item_type
    return item


def _envelope(items: list[dict], module: str = "processing") -> dict:
    offer = {"unit_price": {name: {"total": None, "items": []} for name in ALL_MODULES}}
    offer["unit_price"][module]["items"] = items
    return {"offers": [offer]}


# ---------------------------------------------------------------------------
# parse_location：RC 式与 A1 式
# ---------------------------------------------------------------------------

def test_parse_location_rc_and_a1_styles():
    assert parse_location("page_1!R4C24") == ("page_1", 4, 4, 24, 24)
    assert parse_location("page_1!B7:C7") == ("page_1", 7, 7, 2, 3)
    assert parse_location("B7") == (None, 7, 7, 2, 2)
    assert parse_location("报价单!B28:C28") == ("报价单", 28, 28, 2, 3)
    assert parse_location(None) is None
    assert parse_location("乱七八糟") is None


def test_parse_location_rc_range():
    """RC 区间坐标：'工作表名!R9C3:R9C7'，工作表名可含空格和括号；顺序颠倒也归一。"""
    assert parse_location("CNC5分钟 (2)!R9C3:R9C7") == ("CNC5分钟 (2)", 9, 9, 3, 7)
    assert parse_location("CNC5分钟 (2)!R9C7:R9C3") == ("CNC5分钟 (2)", 9, 9, 3, 7)
    assert parse_location("报价单!R1C1:R3C5") == ("报价单", 1, 3, 1, 5)
    assert parse_location("R9C3:R9C7") == (None, 9, 9, 3, 7)


# ---------------------------------------------------------------------------
# L1 溯源
# ---------------------------------------------------------------------------

def test_l1_amount_found_in_raw_text():
    assert validate_l1_traceability(_envelope([_item(2.01, "损管利税 2.01")]), _ir()) == []


def test_l1_amount_notation_variants_match():
    """写法变体（词边界口径）：0.3 命中 "0.30"，2.1 命中 "2.10"。"""
    env = _envelope([
        _item(0.3, "镭雕 0.30"),
        _item(2.1, "CNC 2.10"),
    ])
    assert validate_l1_traceability(env, _ir()) == []


def test_l1_amount_word_boundary_no_false_positive():
    """词边界口径：0.3 不误中 "10.30"，0 不误中 "13.0"（税率格只有文本时非税 0 豁免）。"""
    env = _envelope([_item(0.3, "合计 10.30")])
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["amount_not_in_evidence"]


def test_l1_zero_amount_exempt_for_non_tax():
    """非税费条目金额为 0 → 豁免金额出处校验（0 在单据里到处出现，必然误报）。"""
    env = _envelope([_item(0.0, "备注 免费赠送")])
    assert validate_l1_traceability(env, _ir()) == []


def test_l1_amount_not_in_evidence():
    env = _envelope([_item(9.99, "CNC (两夹) 1.95")])
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["amount_not_in_evidence"]
    issue = issues[0]
    assert issue.blame == "A" and issue.level == "error"
    assert issue.actual == 9.99 and "9.99" in issue.detail
    assert issue.path == "offers[0].unit_price.processing.items[0].amount_per_pc"
    assert issue.evidence["raw_text"] == "CNC (两夹) 1.95"


def test_l1_tax_amount_not_in_evidence_special_code():
    """税费特例：金额不在 raw_text（该格只有税率）且 rate 也为空 → 硬失败重试（blame=A）。"""
    env = _envelope([_item(2.01, "税率 13% 13.0%", item_type="税费", name="税费")], module="sga_tax")
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["tax_amount_not_in_evidence"]
    assert issues[0].blame == "A" and issues[0].level == "error"
    assert "null" in issues[0].detail and "税率×未税" in issues[0].detail


def test_l1_tax_amount_with_rate_downgraded_to_warning():
    """税费金额无出处但 rate 非空（脚本能兜底派生）→ amount 强制置 null + warning
    （blame=B，不触发重试，流程继续）。"""
    tax_item = _item(0.0, "价税合计（含税13%）", item_type="税费", name="税费")
    tax_item["rate"] = 0.13
    env = _envelope([tax_item], module="sga_tax")
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["tax_amount_not_in_evidence"]
    issue = issues[0]
    assert issue.blame == "B" and issue.level == "warning"
    assert issue.actual == 0.0 and "已置 null" in issue.detail
    assert tax_item["amount_per_pc"] is None


def test_l1_tax_amount_present_passes():
    env = _envelope([_item(2.01, "税额 2.01", item_type="税费", name="税费")], module="sga_tax")
    assert validate_l1_traceability(env, _ir()) == []


def test_l1_invalid_location_row_out_of_range():
    # ir_chuangfeng 的 page_1 行 1~13、列 1~29
    env = _envelope([_item(3.0, "3.00", location="page_1!R99C9")])
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["invalid_location"]
    assert "超出" in issues[0].detail


def test_l1_invalid_location_unknown_sheet_single_sheet_corrected():
    # 单 sheet IR：sheet 名写错（占位词）不构歧义——就地修正为真实 sheet 名，降级为提示
    item = _item(3.0, "3.00", location="page_2!R4C9")
    env = _envelope([item])
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["invalid_location"]
    assert issues[0].blame == "B" and issues[0].level == "warning"
    assert "已按唯一 sheet" in issues[0].detail
    assert item["evidence"]["location"] == "page_1!R4C9"


def test_l1_invalid_location_unknown_sheet_multi_sheet_hard_error():
    # 多 sheet IR：写错 sheet 名是真歧义 → blame=A 硬错误，报错带合法 sheet 名清单
    ir = IR.from_dict({
        "source_file": "多sheet.xlsx", "file_hash": "h", "file_type": "xlsx",
        "sheets": ["报价单A", "报价单B"], "blocks": [],
        "tables": [
            {"sheet": "报价单A", "row_number": 4,
             "cells": [{"row": 4, "col": c, "value": "x"} for c in range(1, 8)]},
            {"sheet": "报价单B", "row_number": 4,
             "cells": [{"row": 4, "col": c, "value": "y"} for c in range(1, 8)]},
        ],
    })
    env = _envelope([_item(3.0, "3.00", location="sheet!R4C9")])
    issues = validate_l1_traceability(env, ir)
    assert [i.issue for i in issues] == ["invalid_location"]
    assert issues[0].blame == "A"
    assert "不存在于 IR" in issues[0].detail
    assert "报价单A" in issues[0].detail and "报价单B" in issues[0].detail


def test_l1_location_unparseable():
    env = _envelope([_item(3.0, "3.00", location="左上角那块")])
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["invalid_location"]
    assert "无法解析" in issues[0].detail


def test_l1_location_a1_style_in_range_passes():
    env = _envelope([_item(3.0, "3.00", location="page_1!B2:C2")])
    assert validate_l1_traceability(env, _ir()) == []


def test_l1_location_rc_range_with_spaced_sheet_name():
    """脱敏材料2 场景：RC 区间坐标 + 含空格括号的工作表名，落在范围内 → 通过。"""
    ir = IR.from_dict({
        "source_file": "脱敏材料2.xlsx", "file_hash": "h", "file_type": "xlsx",
        "sheets": ["CNC5分钟 (2)"], "blocks": [],
        "tables": [{"sheet": "CNC5分钟 (2)", "row_number": 9,
                    "cells": [{"row": 9, "col": c, "value": "x"} for c in range(1, 8)]}],
    })
    env = _envelope([_item(3.0, "3.00", location="CNC5分钟 (2)!R9C3:R9C7")])
    assert validate_l1_traceability(env, ir) == []
    # 超界仍报 invalid_location
    env = _envelope([_item(3.0, "3.00", location="CNC5分钟 (2)!R9C3:R9C8")])
    issues = validate_l1_traceability(env, ir)
    assert [i.issue for i in issues] == ["invalid_location"]


def test_l1_missing_evidence_is_info_level():
    env = _envelope([{"name": "x", "amount_per_pc": 1.0, "evidence": None}])
    issues = validate_l1_traceability(env, _ir())
    assert [i.issue for i in issues] == ["missing_evidence"]
    assert issues[0].level == "info" and issues[0].blame == "A"


def test_l1_issue_json_serializable():
    env = _envelope([_item(9.99, "CNC (两夹) 1.95")])
    issues = validate_l1_traceability(env, _ir())
    assert json.loads(json.dumps(issues[0].to_dict(), ensure_ascii=False))["issue"] == "amount_not_in_evidence"


# ---------------------------------------------------------------------------
# L0 + 创锋真实幻觉案例
# ---------------------------------------------------------------------------

def test_l0_chuangfeng_fixture_passes():
    assert validate_l0_schema(load_fixture("envelope_chuangfeng_two_offers.json")) == []


def test_l1_chuangfeng_fixture_flags_hallucinated_tax():
    """真实 bug 案例：LLM 把相邻单元格金额抄为税费（offer0 抄 2.01、offer1 抄 1.83），
    金额不出现在自己 evidence.raw_text（『税率 13% 13.0%』『13.0%』）中。因 rate=0.13
    非空，脚本可按税率×未税兜底派生 → 不再硬失败：金额强制置 null，降级 warning（blame=B）。
    rate 也为空时的硬失败路径见 test_l1_tax_amount_not_in_evidence_special_code。"""
    envelope = load_fixture("envelope_chuangfeng_two_offers.json")
    issues = validate_l1_traceability(envelope, _ir())
    tax_issues = [i for i in issues if i.issue == "tax_amount_not_in_evidence"]
    assert {i.actual for i in tax_issues} == {2.01, 1.83}
    assert all(i.blame == "B" and i.level == "warning" for i in tax_issues)
    for issue in tax_issues:
        assert str(issue.actual) not in (issue.evidence.get("raw_text") or "")
    # 幻觉金额已被强制置 null（交 derive 按税率×未税派生）
    for offer in envelope["offers"]:
        tax_item = next(
            i for i in offer["unit_price"]["sga_tax"]["items"] if i.get("item_type") == "税费"
        )
        assert tax_item["amount_per_pc"] is None
    # 非税费条目（含被抄的损耗 2.01 本体）全部通过溯源；坐标全部有效
    assert [i for i in issues if i.issue == "amount_not_in_evidence"] == []
    assert [i for i in issues if i.issue == "invalid_location"] == []
    assert [i for i in issues if i.issue == "missing_evidence"] == []


# ---------------------------------------------------------------------------
# L2 勾稽（blame=B，只记录）
# ---------------------------------------------------------------------------

def _l2_envelope(processing_total=None, processing_items=None, sga_items=None,
                 summary=None) -> dict:
    up = {name: {"total": None, "items": []} for name in ALL_MODULES}
    up["processing"] = {"total": processing_total, "items": processing_items or []}
    up["sga_tax"] = {"total": None, "items": sga_items or []}
    up["summary"] = summary or {}
    return {"offers": [{"unit_price": up}]}


def test_l2_module_total_mismatch():
    env = _l2_envelope(processing_total=4.0, processing_items=[_item(2.0, "a 2"), _item(1.2, "b 1.2")])
    issues = validate_l2_reconcile(env)
    assert [i.issue for i in issues] == ["module_total_mismatch"]
    assert issues[0].blame == "B"
    assert issues[0].expected == 3.2 and issues[0].actual == 4.0


def test_l2_module_total_uses_deduped_sum():
    """口径同 derive：共享单元格去重后 total==Σitems，不报（去重场景不误报）。"""
    items = [
        _item(0.3, "镭雕 0.30", location="page_1!R4C18", name="镭雕"),
        _item(0.3, "全检 0.30", location="page_1!R4C18", name="全检"),
    ]
    env = _l2_envelope(processing_total=0.3, processing_items=items)
    assert validate_l2_reconcile(env) == []


def test_l2_summary_not_closed():
    summary = {"untaxed_total": 999.0, "tax_amount": None, "taxed_total": None,
               "discount": None, "final_unit_price_taxed": None}
    env = _l2_envelope(processing_total=3.2, processing_items=[_item(2.0, "a 2"), _item(1.2, "b 1.2")],
                       summary=summary)
    issues = validate_l2_reconcile(env)
    assert [i.issue for i in issues] == ["summary_not_closed"]
    assert issues[0].path.endswith("summary.untaxed_total")
    assert issues[0].expected == 3.2 and issues[0].actual == 999.0
    assert issues[0].blame == "B"


def test_l2_tax_rate_mismatch():
    tax_item = _item(2.01, "税额 2.01", item_type="税费", name="税费")
    tax_item["rate"] = 0.13
    env = _l2_envelope(processing_total=3.2, processing_items=[_item(2.0, "a 2"), _item(1.2, "b 1.2")],
                       sga_items=[tax_item],
                       summary={"untaxed_total": 3.2, "tax_amount": 2.01, "taxed_total": 5.21,
                                "discount": None, "final_unit_price_taxed": 5.21})
    issues = validate_l2_reconcile(env)
    assert [i.issue for i in issues] == ["tax_rate_mismatch"]
    # 13% × 未税 3.2 = 0.416
    assert issues[0].expected == pytest.approx(0.416)
    assert issues[0].actual == 2.01


def test_l2_possible_shared_cell_warning():
    """同 location 同金额但 raw_text 非恰好出现一次：derive 不会去重，L2 报疑似同值重复（warning）。"""
    items = [
        _item(0.3, "镭雕 0.30", location="page_1!R4C18"),
        _item(0.3, "镭雕 0.30 镭雕 0.30", location="page_1!R4C18"),
    ]
    env = _l2_envelope(processing_total=0.6, processing_items=items)
    issues = validate_l2_reconcile(env)
    shared = [i for i in issues if i.issue == "possible_shared_cell"]
    assert len(shared) == 1
    assert shared[0].level == "warning" and shared[0].blame == "B"


def test_l2_clean_envelope_no_issues():
    items = [_item(2.0, "a 2"), _item(1.2, "b 1.2")]
    tax_item = _item(0.416, "税额 0.42", item_type="税费", name="税费")
    tax_item["rate"] = 0.13
    env = _l2_envelope(processing_total=3.2, processing_items=items, sga_items=[tax_item],
                       summary={"untaxed_total": 3.2, "tax_amount": 0.416, "taxed_total": 3.616,
                                "discount": None, "final_unit_price_taxed": 3.616})
    assert validate_l2_reconcile(env) == []


# ---------------------------------------------------------------------------
# run_validators：统一入口 + L0 短路
# ---------------------------------------------------------------------------

def test_run_validators_l0_short_circuits():
    """信封结构坏（无 offers）→ 只报 L0，不跑 L1（深层校验无意义）。"""
    issues = run_validators({"unit_price": {}}, _ir(), levels=("L0", "L1"))
    assert issues and all(i.issue == "schema_invalid" for i in issues)


def test_run_validators_default_levels():
    envelope = load_fixture("envelope_chuangfeng_two_offers.json")
    issues = run_validators(envelope, _ir())
    # L0 通过；L1 报出两个 offer 的幻觉税费（rate 非空 → 降级 warning，不触发重试）
    assert all(i.issue == "tax_amount_not_in_evidence" for i in issues)
    assert all(i.level == "warning" for i in issues)
    assert len(issues) == 2


def test_run_validators_all_levels():
    envelope = load_fixture("envelope_chuangfeng_two_offers.json")
    issues = run_validators(envelope, _ir(), levels=("L0", "L1", "L2"))
    # L1 幻觉税费降级 warning（blame=B 由脚本兜底）；L2 勾稽问题 blame=B 只记录
    assert any(i.issue == "tax_amount_not_in_evidence" and i.level == "warning" for i in issues)
    assert any(i.issue in ("module_total_mismatch", "summary_not_closed", "tax_rate_mismatch") for i in issues)
    assert all(i.blame == "B" for i in issues)


def test_run_validators_unknown_level_raises():
    with pytest.raises(ValueError):
        run_validators({"offers": []}, _ir(), levels=("L3",))
