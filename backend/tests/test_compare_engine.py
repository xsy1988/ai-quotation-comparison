"""compare_engine 机械对比：层级空值语义、抽屉归组（含兜底桶）、指纹对齐、模治具、警示汇总。"""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.compare.compare_engine import get_comparison
from app.db import get_connection, init_db
from app.persist import persist_quote

BACKEND_DIR = Path(__file__).resolve().parent.parent


def make_quote(supplier: str, with_inspection: bool, with_extras: bool) -> dict:
    processing_items = [
        {
            "name": "CNC加工",
            "amount_per_pc": 2.0 if with_extras else 1.8,
            "atom_code": "AT-QX-001",
            "is_new_process": False,
            "bundle_flag": False,
            "bundle_fingerprint": "AT-QX-001",
            "confidence": "high",
            "match_path": "alias_exact",
            "confirm_status": "unconfirmed",
            "note": None,
        },
        {
            "name": "阳极氧化",
            "amount_per_pc": 1.2,
            "atom_code": "AT-ZH-013",
            "is_new_process": False,
            "bundle_flag": True,
            "bundle_fingerprint": "AT-QX-001|AT-ZH-013",
            "confidence": "mid",
            "match_path": "llm_semantic",
            "confirm_status": "unconfirmed",
            "note": None,
        },
    ]
    if with_extras:
        processing_items += [
            {
                "name": "激光熔覆",
                "amount_per_pc": 0.8,
                "atom_code": None,
                "is_new_process": False,
                "bundle_flag": False,
                "bundle_fingerprint": None,
                "confidence": "low",
                "match_path": None,
                "confirm_status": "unconfirmed",
                "note": "完全无法判断",
            },
            {
                "name": "等离子抛光",
                "amount_per_pc": 0.6,
                "atom_code": None,
                "is_new_process": True,
                "bundle_flag": False,
                "bundle_fingerprint": None,
                "confidence": "low",
                "match_path": "llm_semantic",
                "confirm_status": "unconfirmed",
                "note": "清单外工艺",
            },
        ]
    else:
        processing_items.append(
            {
                "name": "喷砂",
                "amount_per_pc": 0.4,
                "atom_code": "AT-ZP-005",
                "is_new_process": False,
                "bundle_flag": False,
                "bundle_fingerprint": "AT-ZP-005",
                "confidence": "mid",
                "match_path": "alias_exact",
                "confirm_status": "unconfirmed",
                "note": None,
            }
        )

    materials_total = 5.0 if with_extras else 4.0
    processing_total = 4.6 if with_extras else 3.4
    inspection = (
        {"total": 0.3, "items": [{"name": "全尺寸检验", "amount_per_pc": 0.3, "note": None}]}
        if with_inspection
        else {"total": None, "items": []}
    )
    packaging_total = 0.15 if with_extras else 0.2
    sga_items = (
        [
            {"name": "损耗", "amount_per_pc": 0.1, "item_type": "损耗", "rate": None, "note": None},
            {"name": "管理费", "amount_per_pc": 0.2, "item_type": "管理费", "rate": None, "note": None},
            {"name": "利润", "amount_per_pc": 0.3, "item_type": "利润", "rate": None, "note": None},
            {"name": "增值税", "amount_per_pc": 0.46, "item_type": "税费", "rate": 0.13, "note": None},
        ]
        if with_extras
        else [
            {"name": "管理费", "amount_per_pc": 0.3, "item_type": "管理费", "rate": None, "note": None},
            {"name": "利润", "amount_per_pc": 0.3, "item_type": "利润", "rate": None, "note": None},
            {"name": "增值税", "amount_per_pc": 0.3, "item_type": "税费", "rate": 0.13, "note": None},
        ]
    )
    sga_total = round(sum(i["amount_per_pc"] for i in sga_items), 6)
    untaxed = round(materials_total + processing_total + (0.3 if with_inspection else 0) + packaging_total + (sga_total - sga_items[-1]["amount_per_pc"]), 6)
    tax = sga_items[-1]["amount_per_pc"]
    taxed = round(untaxed + tax, 6)
    discount = 0.5 if with_extras else None
    final = round(taxed - (discount or 0), 6)

    quote = {
        "schema_version": "1.1",
        "supplier": {"supplier_name": supplier, "supplier_code": None},
        "basic": {"part_name": "对比零件", "currency": "CNY", "category": None},
        "unit_price": {
            "materials": {
                "total": materials_total,
                "items": [{"name": "铝材", "amount_per_pc": materials_total, "spec": None, "note": None}],
            },
            "processing": {"total": processing_total, "items": processing_items},
            "inspection": inspection,
            "packaging_transport": {"total": packaging_total, "items": []},
            "sga_tax": {"total": sga_total, "items": sga_items},
            "other": {"total": 0.0, "items": []},
            "summary": {
                "untaxed_total": untaxed,
                "tax_amount": tax,
                "taxed_total": taxed,
                "discount": discount,
                "final_unit_price_taxed": final,
                "calc_check": "unchecked",
            },
        },
        "tooling": {
            "total": 28000 if with_extras else 15000,
            "molds": {
                "total": 25000 if with_extras else 15000,
                "items": [
                    {
                        "name": "冲压成型模" if with_extras else "简易模",
                        "amount": 25000 if with_extras else 15000,
                        "cavities": 1 if with_extras else 2,
                        "lifespan": 500000 if with_extras else 300000,
                        "note": None,
                    }
                ],
            },
            "fixtures": (
                {
                    "total": 3000,
                    "items": [{"name": "检具", "amount": 3000, "note": None}],
                }
                if with_extras
                else {"total": None, "items": []}
            ),
            "stencils": {"total": None, "items": []},
        },
    }
    return quote


@pytest.fixture
def comparison(tmp_path, monkeypatch):
    """灌主数据 + 两个供应商报价单（A 全量含未匹配/新工艺，B 缺检验）。"""
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    db_path = tmp_path / "cmp.db"
    monkeypatch.setenv("QUOTES_DB_PATH", str(db_path))
    env = {**os.environ, "QUOTES_DB_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    conn = get_connection()
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('对比测试', 'parsed')")
        task_id = cur.lastrowid
    qa = persist_quote(make_quote("供应商A", True, True), task_id=task_id, file_hash="ha")
    qb = persist_quote(make_quote("供应商B", False, False), task_id=task_id, file_hash="hb")
    result = get_comparison(conn, task_id)
    return conn, task_id, qa["quote_id"], qb["quote_id"], result


def _hierarchy_row(result, key):
    return next(r for r in result["hierarchy"] if r["key"] == key)


def _tree_node(result, key):
    return next(n for n in result["price_tree"] if n["key"] == key)


def _child(node, key):
    return next(c for c in node["children"] if c["key"] == key)


def test_price_tree_basic_info(comparison):
    conn, task_id, qa, qb, result = comparison
    basic = {b["quote_id"]: b for b in result["basic"]}
    assert basic[qa]["part_name"] == "对比零件"
    assert basic[qa]["currency"] == "CNY"
    assert basic[qb]["moq"] is None  # make_quote 未填 moq → None
    basic_node = _tree_node(result, "basic")
    assert [c["label"] for c in basic_node["children"]] == [
        "项目名称", "零件名称", "材料规格", "报价时间", "币种", "最小起订量",
    ]
    part_row = _child(basic_node, "basic_part_name")
    assert part_row["kind"] == "text"
    assert part_row["values"] == {qa: "对比零件", qb: "对比零件"}
    conn.close()


def test_price_tree_summary_rows(comparison):
    conn, task_id, qa, qb, result = comparison
    unit = _tree_node(result, "unit_price")
    assert [c["key"] for c in unit["children"]][:3] == ["final", "untaxed", "discount"]
    assert _child(unit, "final")["values"][qa] == 10.61
    assert _child(unit, "final")["label"] == "计算总价（含税）"
    assert _child(unit, "discount")["values"][qb] is None
    assert _child(unit, "untaxed")["values"][qa] is not None
    conn.close()


def test_price_tree_sga_tax_split(comparison):
    """损管利 = sga_tax 合计 − 税费；税费单列一组带税率。"""
    conn, task_id, qa, qb, result = comparison
    unit = _tree_node(result, "unit_price")
    # A：损耗0.1+管理费0.2+利润0.3+增值税0.46 → 损管利 0.6；B：管理费0.3+利润0.3+增值税0.3 → 损管利 0.6
    sga = _child(unit, "sga")
    assert sga["values"] == {qa: 0.6, qb: 0.6}
    tax = _child(unit, "tax")
    assert tax["values"] == {qa: 0.46, qb: 0.3}
    tax_children = {c["label"]: c for c in tax["children"]}
    assert set(tax_children) == {"税费"}  # 按 item_type 归一，原文"增值税"不再单独成行
    assert tax_children["税费"]["meta"][qa]["rate"] == 0.13
    # 损管利明细只含 损耗/管理费/利润/其他，不含税费
    sga_names = {c["label"] for c in sga["children"]}
    assert "增值税" not in sga_names
    assert {"损耗", "管理费", "利润"} <= sga_names
    mgmt = {c["label"]: c for c in sga["children"]}["管理费"]
    assert mgmt["meta"][qb]["item_type"] == "管理费"
    conn.close()


def test_price_tree_sga_all_tax_becomes_none(tmp_path, monkeypatch):
    """供应商 sga_tax 合计全部来自税费条目时，损管利行该供应商为 None（不是 0）。"""
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "sga.db"))
    init_db()
    conn = get_connection()
    quote = make_quote("供应商T", False, False)
    for item in quote["unit_price"]["processing"]["items"]:
        item["atom_code"] = None  # 本用例不灌主数据，避免 atom 外键
    quote["unit_price"]["sga_tax"] = {
        "total": 0.3,
        "items": [{"name": "增值税", "amount_per_pc": 0.3, "item_type": "税费", "rate": 0.13, "note": None}],
    }
    quote["unit_price"]["summary"]["tax_amount"] = 0.3
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    qid = persist_quote(quote, task_id=task_id, file_hash="ht")["quote_id"]
    result = get_comparison(conn, task_id)
    sga = _child(_tree_node(result, "unit_price"), "sga")
    assert sga["values"][qid] is None
    conn.close()


def test_price_tree_sga_total_tax_only_falls_back_to_items(tmp_path, monkeypatch):
    """模块合计恰等于税费合计、但损管利明细有金额时（total 只落税费，如惠州豪泽单），
    损管利行回退取非税费明细合计，而不是误报未报。"""
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "sga2.db"))
    init_db()
    conn = get_connection()
    quote = make_quote("供应商T2", False, False)
    for item in quote["unit_price"]["processing"]["items"]:
        item["atom_code"] = None  # 本用例不灌主数据，避免 atom 外键
    quote["unit_price"]["sga_tax"] = {
        "total": 2.5635,
        "items": [
            {"name": "不良率10%", "amount_per_pc": 1.56, "item_type": "损耗", "rate": 0.1, "note": None},
            {"name": "管理5%", "amount_per_pc": 0.78, "item_type": "管理费", "rate": 0.05, "note": None},
            {"name": "利润", "amount_per_pc": 1.19, "item_type": "利润", "rate": None, "note": None},
            {"name": "税费13%", "amount_per_pc": 2.5635, "item_type": "税费", "rate": 0.13, "note": None},
        ],
    }
    quote["unit_price"]["summary"]["tax_amount"] = 2.5635
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    qid = persist_quote(quote, task_id=task_id, file_hash="ht2")["quote_id"]
    result = get_comparison(conn, task_id)
    sga = _child(_tree_node(result, "unit_price"), "sga")
    assert sga["values"][qid] == round(1.56 + 0.78 + 1.19, 6)
    conn.close()


def test_price_tree_material_name_falls_back_to_spec(tmp_path, monkeypatch):
    """材料条目名为栏目名（原材料/材料费等）时，展示名回退用 basic.material_spec（真实牌号）。"""
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "mat.db"))
    init_db()
    conn = get_connection()
    quote = make_quote("供应商M", False, False)
    for item in quote["unit_price"]["processing"]["items"]:
        item["atom_code"] = None  # 本用例不灌主数据，避免 atom 外键
    quote["basic"]["material_spec"] = "ADC12铝合金"
    quote["unit_price"]["materials"]["items"] = [
        {"name": "原材料", "amount_per_pc": 1.22, "spec": None, "note": None},
    ]
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    qid = persist_quote(quote, task_id=task_id, file_hash="hm")["quote_id"]
    result = get_comparison(conn, task_id)
    materials = _child(_tree_node(result, "unit_price"), "materials")
    row = materials["children"][0]
    assert row["meta"][qid]["name"] == "ADC12铝合金"  # 栏目名 → 真实材料牌号
    # 具体材料名不受影响
    conn2 = conn
    quote2 = make_quote("供应商M2", False, False)
    for item in quote2["unit_price"]["processing"]["items"]:
        item["atom_code"] = None
    quote2["basic"]["material_spec"] = "ADC12铝合金"
    quote2["unit_price"]["materials"]["items"] = [
        {"name": "铝合金ADC12", "amount_per_pc": 1.21, "spec": None, "note": None},
    ]
    qid2 = persist_quote(quote2, task_id=task_id, file_hash="hm2")["quote_id"]
    result = get_comparison(conn2, task_id)
    materials = _child(_tree_node(result, "unit_price"), "materials")
    row = materials["children"][0]
    assert row["meta"][qid2]["name"] == "铝合金ADC12"
    conn.close()


def test_price_tree_detail_union_and_null(comparison):
    """明细 children 跨供应商并集；某供应商缺该条目 → null。
    加工费按原子码对齐（行键 processing::atom:<code>），材料费按位置对齐（行键 materials::<序号>）。"""
    conn, task_id, qa, qb, result = comparison
    unit = _tree_node(result, "unit_price")
    materials = _child(unit, "materials")
    alum = _child(materials, "materials::0")  # 两家的第一种材料同行，各自名称在 meta.name
    assert alum["values"] == {qa: 5.0, qb: 4.0}
    assert alum["meta"][qa]["name"] == "铝材"
    assert alum["meta"][qb]["name"] == "铝材"
    processing = _child(unit, "processing")
    names = {c["label"] for c in processing["children"]}
    # 标签取原子主数据名称；未匹配/清单外条目按原文名称归组
    assert {"CNC加工", "阳极氧化", "激光熔覆", "等离子抛光", "喷砂"} == names
    laser = _child(processing, "processing::name:激光熔覆")  # 未匹配条目按归一化名称对齐，仅 A 报
    assert laser["values"] == {qa: 0.8, qb: None}
    sand = _child(processing, "processing::atom:AT-ZP-005")  # 仅 B 报
    assert sand["values"] == {qa: None, qb: 0.4}
    cnc = _child(processing, "processing::atom:AT-QX-001")
    assert cnc["meta"][qa]["atom_code"] == "AT-QX-001"
    assert cnc["meta"][qa]["match_path"] == "L1_alias"
    assert cnc["meta"][qb]["atom_code"] == "AT-QX-001"
    conn.close()


def _seed_master(tmp_path, monkeypatch, name: str):
    """导入原子主数据并建空库，返回 conn（加工条目的 atom_code 有外键依赖）。"""
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / f"snapshots-{name}")
    db_path = tmp_path / f"{name}.db"
    monkeypatch.setenv("QUOTES_DB_PATH", str(db_path))
    env = {**os.environ, "QUOTES_DB_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    return get_connection()


def test_price_tree_processing_casefold_merge(tmp_path, monkeypatch):
    """同一原子码、原文大小写不同（CNC/cnc）→ 同一行对比，标签取原子主数据名称。"""
    conn = _seed_master(tmp_path, monkeypatch, "case")
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    qa = make_quote("供应商A", False, False)
    qb = make_quote("供应商B", False, False)
    qa["unit_price"]["processing"]["items"][0]["name"] = "CNC"
    qb["unit_price"]["processing"]["items"][0]["name"] = "cnc"
    qa_id = persist_quote(qa, task_id=task_id, file_hash="hc1")["quote_id"]
    qb_id = persist_quote(qb, task_id=task_id, file_hash="hc2")["quote_id"]
    result = get_comparison(conn, task_id)
    processing = _child(_tree_node(result, "unit_price"), "processing")
    cnc_rows = [c for c in processing["children"] if c["label"] == "CNC加工"]
    assert len(cnc_rows) == 1  # 大小写不同不再拆行
    row = cnc_rows[0]
    assert row["key"] == "processing::atom:AT-QX-001"
    assert row["values"] == {qa_id: 1.8, qb_id: 1.8}
    assert row["meta"][qa_id]["name"] == "CNC"
    assert row["meta"][qb_id]["name"] == "cnc"
    conn.close()


def test_price_tree_sga_grouped_by_item_type(tmp_path, monkeypatch):
    """损管利明细按 item_type 归一到固定结构：不良率→损耗、管理/管理费用→管理费，不再按原文拆行。"""
    conn = _seed_master(tmp_path, monkeypatch, "sga")
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    qa = make_quote("供应商A", False, False)
    qb = make_quote("供应商B", False, False)
    qa["unit_price"]["sga_tax"] = {
        "total": 0.51,
        "items": [
            {"name": "损耗", "amount_per_pc": 0.15, "item_type": "损耗", "rate": None, "note": None},
            {"name": "管理费用", "amount_per_pc": 0.3, "item_type": "管理费", "rate": None, "note": None},
            {"name": "利润", "amount_per_pc": 0.9, "item_type": "利润", "rate": None, "note": None},
            {"name": "税费", "amount_per_pc": 0.51, "item_type": "税费", "rate": 0.13, "note": None},
        ],
    }
    qb["unit_price"]["sga_tax"] = {
        "total": 5.5,
        "items": [
            {"name": "不良率", "amount_per_pc": 1.68, "item_type": "损耗", "rate": 0.1, "note": None},
            {"name": "管理", "amount_per_pc": 0.93, "item_type": "管理费", "rate": 0.05, "note": None},
            {"name": "利润", "amount_per_pc": 1.19, "item_type": "利润", "rate": 0.1, "note": None},
            {"name": "税费", "amount_per_pc": 2.7, "item_type": "税费", "rate": 0.13, "note": None},
        ],
    }
    qa_id = persist_quote(qa, task_id=task_id, file_hash="hs1")["quote_id"]
    qb_id = persist_quote(qb, task_id=task_id, file_hash="hs2")["quote_id"]
    result = get_comparison(conn, task_id)
    unit = _tree_node(result, "unit_price")
    sga = _child(unit, "sga")
    assert [c["label"] for c in sga["children"]] == ["损耗", "管理费", "利润"]  # 固定结构顺序
    loss = _child(sga, "sga::损耗")
    assert loss["values"] == {qa_id: 0.15, qb_id: 1.68}
    assert loss["meta"][qb_id]["name"] == "不良率"  # 原文保留在 meta
    assert loss["meta"][qb_id]["rate"] == 0.1
    mgmt = _child(sga, "sga::管理费")
    assert mgmt["values"] == {qa_id: 0.3, qb_id: 0.93}
    tax = _child(unit, "tax")
    assert [c["label"] for c in tax["children"]] == ["税费"]
    conn.close()


def test_price_tree_materials_align_by_position(tmp_path, monkeypatch):
    """材料名称自由文本不同（铝合金/3#材料）→ 按各家报价内位置同一行，meta.name 带各家原名。"""
    conn = _seed_master(tmp_path, monkeypatch, "mat")
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    qa = make_quote("供应商A", False, False)
    qb = make_quote("供应商B", False, False)
    qa["unit_price"]["materials"] = {
        "total": 1.99,
        "items": [{"name": "铝合金", "amount_per_pc": 1.99, "spec": None, "note": "净重37.9g"}],
    }
    qb["unit_price"]["materials"] = {
        "total": 0.7,
        "items": [{"name": "3#材料", "amount_per_pc": 0.7, "spec": None, "note": None}],
    }
    qa_id = persist_quote(qa, task_id=task_id, file_hash="hm1")["quote_id"]
    qb_id = persist_quote(qb, task_id=task_id, file_hash="hm2")["quote_id"]
    result = get_comparison(conn, task_id)
    materials = _child(_tree_node(result, "unit_price"), "materials")
    assert len(materials["children"]) == 1  # 各自只有一种材料 → 同一行
    row = materials["children"][0]
    assert row["label"] == "材料 1"
    assert row["values"] == {qa_id: 1.99, qb_id: 0.7}
    assert row["meta"][qa_id]["name"] == "铝合金"
    assert row["meta"][qb_id]["name"] == "3#材料"
    conn.close()


def test_price_tree_other_conditional(tmp_path, monkeypatch):
    """其他费用组：仅当某供应商模块合计非 None 或有明细行时出现。"""
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "other.db"))
    init_db()
    conn = get_connection()
    quote = make_quote("供应商O", False, False)
    for item in quote["unit_price"]["processing"]["items"]:
        item["atom_code"] = None  # 本用例不灌主数据，避免 atom 外键
    quote["unit_price"]["other"] = {"total": None, "items": []}
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    persist_quote(quote, task_id=task_id, file_hash="ho")
    result = get_comparison(conn, task_id)
    unit = _tree_node(result, "unit_price")
    assert all(c["key"] != "other" for c in unit["children"])
    conn.close()


def test_price_tree_processing_scope_meta(comparison):
    """加工费明细行带 scope_meta（工艺域/阶段/类别）；未匹配条目为 None。"""
    conn, task_id, qa, qb, result = comparison
    processing = _child(_tree_node(result, "unit_price"), "processing")
    cnc = _child(processing, "processing::atom:AT-QX-001")
    assert cnc["scope_meta"]["domain"] == {"code": "QX", "name": "切削"}
    assert cnc["scope_meta"]["stage"] == {"code": "机加", "name": "机加"}
    assert cnc["scope_meta"]["class"] == {"code": "后工序", "name": "后工序"}
    laser = _child(processing, "processing::name:激光熔覆")  # 未匹配
    assert laser["scope_meta"] is None
    # 清单外新工艺（atom_code 空 + is_new_process）同样无 scope_meta，前端分组进"未匹配"组
    plasma = _child(processing, "processing::name:等离子抛光")
    assert plasma["scope_meta"] is None
    conn.close()


def test_processing_shared_cell_is_shared_flag(tmp_path, monkeypatch):
    """共享单元格去重置零副本（note 带共享单元格标记且金额已置 0）→ 行数据带 is_shared，
    前端金额列显示"/"；金额非 0 的同格式 note（用户改过金额）不误标。"""
    conn = _seed_master(tmp_path, monkeypatch, "shared")
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('t', 'parsed')")
        task_id = cur.lastrowid
    quote = make_quote("供应商S", False, False)
    items = quote["unit_price"]["processing"]["items"]
    items[0]["amount_per_pc"] = 0.0
    items[0]["note"] = "与『镭雕』共享单元格 page_1!R4C18，金额只计一次"
    items[1]["note"] = "与『X』共享单元格 p1!R1，金额只计一次"  # 金额非 0 → 不标记
    qid = persist_quote(quote, task_id=task_id, file_hash="hshared")["quote_id"]
    result = get_comparison(conn, task_id)

    details = {d["quote_id"]: d for d in result["processing_details"]}
    by_name = {i["name"]: i for i in details[qid]["items"]}
    assert by_name["CNC加工"]["is_shared"] is True
    assert by_name["阳极氧化"]["is_shared"] is False

    processing = _child(_tree_node(result, "unit_price"), "processing")
    cnc = _child(processing, "processing::atom:AT-QX-001")
    assert cnc["meta"][qid]["is_shared"] is True
    anode = _child(processing, "processing::atom:AT-ZH-013")
    assert anode["meta"][qid]["is_shared"] is False
    conn.close()


def test_price_tree_group_totals(comparison):
    """两个汇总分组行带合计：产品单价（含税）=最终含税单价，模/治具费用=tooling_total。"""
    conn, task_id, qa, qb, result = comparison
    assert _tree_node(result, "unit_price")["values"] == {qa: 10.61, qb: 8.5}
    assert _tree_node(result, "tooling")["values"] == {qa: 28000, qb: 15000}
    conn.close()


def test_price_tree_tooling_children(comparison):
    conn, task_id, qa, qb, result = comparison
    tooling = _tree_node(result, "tooling")
    assert [c["label"] for c in tooling["children"]] == ["模具费", "治具费", "钢网费"]
    mold = _child(tooling, "tooling_mold")
    assert mold["values"] == {qa: 25000, qb: 15000}
    mold_items = {c["label"]: c for c in mold["children"]}
    assert mold_items["冲压成型模"]["meta"][qa]["cavity_count"] == 1
    assert mold_items["冲压成型模"]["meta"][qa]["lifespan"] == 500000
    assert mold_items["简易模"]["values"][qb] == 15000
    fixture = _child(tooling, "tooling_fixture")
    assert fixture["values"] == {qa: 3000, qb: None}
    stencil = _child(tooling, "tooling_stencil")
    assert stencil["values"] == {qa: None, qb: None}
    assert "children" not in stencil
    conn.close()


def test_suppliers_list(comparison):
    conn, task_id, qa, qb, result = comparison
    assert [s["supplier_name"] for s in result["suppliers"]] == ["供应商A", "供应商B"]
    by_id = {s["quote_id"]: s for s in result["suppliers"]}
    assert by_id[qa]["final_unit_price_taxed"] is not None
    assert by_id[qb]["calc_check"] == "pass"
    conn.close()


def test_hierarchy_values(comparison):
    conn, task_id, qa, qb, result = comparison
    assert _hierarchy_row(result, "materials")["values"] == {qa: 5.0, qb: 4.0}
    assert _hierarchy_row(result, "processing")["values"] == {qa: 4.6, qb: 3.4}
    assert _hierarchy_row(result, "final_unit_price_taxed")["values"][qa] == 10.61
    conn.close()


def test_hierarchy_null_semantics(comparison):
    """B 未报检验费 → None，绝不是 0。"""
    conn, task_id, qa, qb, result = comparison
    inspection = _hierarchy_row(result, "inspection")["values"]
    assert inspection[qa] == 0.3
    assert inspection[qb] is None
    discount = _hierarchy_row(result, "discount")["values"]
    assert discount[qb] is None
    conn.close()


def test_drawers_grouping_and_fallback_buckets(comparison):
    conn, task_id, qa, qb, result = comparison
    assert [d["scope"] for d in result["drawers"]] == ["process_domain", "process_stage", "process_class"]

    # CNC 所属的内置域桶：A 2.0 + B 1.8
    conn_db = get_connection()
    domain_groups = conn_db.execute(
        "SELECT group_code, group_name, member_atoms FROM dim_group WHERE scope='process_domain'"
    ).fetchall()
    cnc_group = next(g for g in domain_groups if "AT-QX-001" in json.loads(g["member_atoms"]))
    conn_db.close()

    domain_drawer = result["drawers"][0]
    bucket = next(g for g in domain_drawer["groups"] if g["group_code"] == cnc_group["group_code"])
    assert bucket["values"] == {qa: 2.0, qb: 1.8}
    assert bucket["is_fallback_bucket"] is False

    # 兜底桶只剩"未匹配"：取数口径 atom_code IS NULL（清单外/新工艺条目并入未匹配）
    unmatched = next(g for g in domain_drawer["groups"] if g["group_code"] == "unmatched")
    assert unmatched["is_fallback_bucket"] is True
    # A：激光熔覆 0.8 + 等离子抛光 0.6（均 atom_code NULL）
    assert unmatched["values"] == {qa: 1.4, qb: None}
    # "其它工艺"兜底原子已移除，不存在专属兜底桶
    assert all(g["group_code"] != "other_process" for d in result["drawers"] for g in d["groups"])
    # builtin:domain:QT 的唯一成员曾是已移除的兜底原子 → 该组不下发（避免与未匹配桶重复计数）
    assert all(
        g["group_code"] != "builtin:domain:QT" for d in result["drawers"] for g in d["groups"]
    )
    conn.close()


def test_drawers_null_for_supplier_without_process(comparison):
    """B 路线中没有未匹配/新工艺条目 → 兜底桶对 B 为 None（未含），不是 0。"""
    conn, task_id, qa, qb, result = comparison
    for drawer in result["drawers"]:
        unmatched = next(g for g in drawer["groups"] if g["group_code"] == "unmatched")
        assert unmatched["values"][qb] is None
    conn.close()


def test_fingerprint_groups(comparison):
    conn, task_id, qa, qb, result = comparison
    fps = {g["fingerprint"]: g for g in result["fingerprint_groups"]}
    single = fps["AT-QX-001"]
    assert {r["quote_id"] for r in single["rows"]} == {qa, qb}
    assert sum(1 for r in single["rows"] if r["quote_id"] == qa) == 1
    bundle = fps["AT-QX-001|AT-ZH-013"]
    assert {r["quote_id"] for r in bundle["rows"]} == {qa, qb}
    assert "AT-ZP-005" in fps  # 仅 B 的单项指纹也成组
    # 指纹要连原子名称一起给（前端不展示代号，人记不住）
    assert [a["code"] for a in single["atoms"]] == ["AT-QX-001"]
    assert single["atoms"][0]["name"]
    assert [a["code"] for a in bundle["atoms"]] == ["AT-QX-001", "AT-ZH-013"]
    assert all(a["name"] for a in bundle["atoms"])
    conn.close()


def test_fingerprint_atoms_unknown_code(comparison):
    """指纹里的编码不在原子主数据里（历史脏数据）时，名称给 None 而不是抛错。"""
    conn, task_id, qa, qb, _ = comparison
    with conn:
        conn.execute(
            "UPDATE quote_line SET fingerprint = 'AT-XX-999'"
            " WHERE quote_id = ? AND atom_code = 'AT-QX-001'",
            (qa,),
        )
    result = get_comparison(conn, task_id)
    group = next(g for g in result["fingerprint_groups"] if g["fingerprint"] == "AT-XX-999")
    assert group["atoms"] == [{"code": "AT-XX-999", "name": None}]
    conn.close()


def test_tooling_comparison(comparison):
    conn, task_id, qa, qb, result = comparison
    tooling = {t["type"]: t for t in result["tooling"]}
    molds = {i["quote_id"]: i for i in tooling["mold"]["items"]}
    assert molds[qa]["amount"] == 25000
    assert molds[qa]["cavity_count"] == 1
    assert molds[qa]["lifespan"] == 500000
    assert molds[qb]["amount"] == 15000
    assert molds[qb]["cavity_count"] == 2
    fixtures = {i["quote_id"]: i for i in tooling["fixture"]["items"]}
    assert set(fixtures) == {qa}  # B 未报治具 → 不出现在 items 里
    assert tooling["stencil"]["items"] == []
    conn.close()


def test_processing_details(comparison):
    conn, task_id, qa, qb, result = comparison
    details = {d["quote_id"]: d for d in result["processing_details"]}
    items_a = {i["name"]: i for i in details[qa]["items"]}
    assert details[qa]["module"] == "processing"
    assert len(items_a) == 4
    assert items_a["CNC加工"]["atom_code"] == "AT-QX-001"
    assert items_a["阳极氧化"]["bundle_flag"] is True
    assert items_a["等离子抛光"]["is_new_process"] is True
    assert items_a["等离子抛光"]["atom_code"] is None
    assert items_a["激光熔覆"]["atom_code"] is None
    assert len(details[qb]["items"]) == 3
    conn.close()


def test_warnings_line_counts(comparison):
    conn, task_id, qa, qb, result = comparison
    warnings = {w["quote_id"]: w for w in result["warnings"]}
    assert warnings[qa]["line_counts"] == {"low_confidence": 2, "unmatched": 2, "new_process": 1}
    assert set(warnings[qa]["flags"]) >= {"low_confidence", "unmatched", "new_process"}
    assert warnings[qb]["line_counts"] == {"low_confidence": 0, "unmatched": 0, "new_process": 0}
    conn.close()


def test_empty_task_comparison(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "empty.db"))
    init_db()
    conn = get_connection()
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('空任务', 'parsed')")
    result = get_comparison(conn, cur.lastrowid)
    assert result["suppliers"] == []
    assert all(row["values"] == {} for row in result["hierarchy"])
    assert result["fingerprint_groups"] == []
    conn.close()



def test_custom_drawer_becomes_comparison_tab(comparison):
    """抽屉表新增自定义抽屉 + 该 scope 下建分组 → 比价结果里多出一个页签（名称取抽屉名）。"""
    conn, task_id, qa, qb, result = comparison
    db = get_connection()
    with db:
        db.execute("INSERT INTO drawer (code, name, sort_order) VALUES ('cost_center', '成本中心', 40)")
        db.execute(
            """INSERT INTO dim_group (group_code, group_name, scope, member_atoms)
               VALUES ('g-cost', '切削', 'cost_center', '["AT-QX-001"]')"""
        )
    fresh = get_comparison(db, task_id)
    db.close()

    assert [d["scope"] for d in fresh["drawers"]] == [
        "process_domain", "process_stage", "process_class", "cost_center",
    ]
    assert [d["name"] for d in fresh["drawers"]][-1] == "成本中心"
    custom = fresh["drawers"][-1]
    bucket = next(g for g in custom["groups"] if g["group_code"] == "g-cost")
    assert bucket["values"] == {qa: 2.0, qb: 1.8}
    # 兜底桶仍在，保证未归类金额不丢
    assert custom["groups"][-1]["group_code"] == "unmatched"


def test_task_quotes_group_same_supplier_adjacently(tmp_path, monkeypatch):
    """供应商分组：同一供应商的多份报价必须相邻（首次出现顺序），组内按 quote id 升序。

    历史缺陷：任务内报价按 quote id 全序排列，同一供应商的两份报价被别家插开，
    用户在「报价对比」「AI 分析」里无法相邻比对同一家的报价。
    """
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    db_path = tmp_path / "group.db"
    monkeypatch.setenv("QUOTES_DB_PATH", str(db_path))
    env = {**os.environ, "QUOTES_DB_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    conn = get_connection()
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('分组测试', 'parsed')")
        task_id = cur.lastrowid
    # 上传顺序：甲、乙、甲 → 期望列顺序 甲1、甲2、乙（而不是 甲1、乙、甲2）
    a1 = persist_quote(make_quote("甲供应商", True, True), task_id=task_id, file_hash="g1")
    b1 = persist_quote(make_quote("乙供应商", False, False), task_id=task_id, file_hash="g2")
    a2 = persist_quote(make_quote("甲供应商", False, True), task_id=task_id, file_hash="g3")

    result = get_comparison(conn, task_id)
    order = [s["quote_id"] for s in result["suppliers"]]
    assert order == [a1["quote_id"], a2["quote_id"], b1["quote_id"]]
    # 层级/指纹/模治具等下游模块共用同一顺序（列对齐的前提）
    assert list(_hierarchy_row(result, "materials")["values"]) == order
    assert {s["quote_id"] for g in result["fingerprint_groups"] for s in g["rows"]} == set(order)
    conn.close()


def _set_moq(conn, quote_id: int, moq, moq_options) -> None:
    """直接改 quote.basic_info：模拟已解析报价单里带多档起订量。"""
    row = conn.execute("SELECT basic_info FROM quote WHERE id = ?", (quote_id,)).fetchone()
    info = json.loads(row["basic_info"] or "{}") or {}
    info["moq"] = moq
    info["moq_options"] = moq_options
    with conn:
        conn.execute(
            "UPDATE quote SET basic_info = ? WHERE id = ?",
            (json.dumps(info, ensure_ascii=False), quote_id),
        )


def test_price_tree_moq_options_row_only_when_tiered(comparison):
    """起订量分档：只有真的分档时才多出「起订量分档」行，且排在「最小起订量」之后。"""
    conn, task_id, qa, qb, result = comparison
    basic_node = _tree_node(result, "basic")
    assert "basic_moq_options" not in [c["key"] for c in basic_node["children"]]

    _set_moq(
        conn,
        qa,
        3000,
        [
            {"condition": "皮革现货单色", "value": 3000, "note": None},
            {"condition": "定制皮革单色", "value": 40000, "note": "金属管需提供3%损耗"},
        ],
    )
    result = get_comparison(conn, task_id)
    basic_node = _tree_node(result, "basic")
    assert [c["key"] for c in basic_node["children"]][-2:] == ["basic_moq", "basic_moq_options"]
    row = _child(basic_node, "basic_moq_options")
    assert row["label"] == "起订量分档"
    assert row["kind"] == "text"
    assert row["values"][qa] == "皮革现货单色 3,000\n定制皮革单色 40,000（金属管需提供3%损耗）"
    assert row["values"][qb] is None  # B 没有分档 → 空值语义
    suppliers = {s["quote_id"]: s for s in result["suppliers"]}
    assert suppliers[qa]["moq"] == 3000
    assert suppliers[qa]["moq_options"][1]["value"] == 40000
    assert suppliers[qb]["moq_options"] is None
    conn.close()


def test_moq_option_text_formatting():
    """分档文本：每档一行；不限条件的档写「不限条件」；脏数据整条跳过；无分档返回 None。"""
    from app.compare.compare_engine import moq_option_text

    assert moq_option_text(None) is None
    assert moq_option_text([]) is None
    assert moq_option_text("3000") is None
    assert moq_option_text([{"condition": None, "value": 3000, "note": None}]) == "不限条件 3,000"
    assert moq_option_text(
        [
            {"condition": "现货", "value": 3000},
            {"condition": "定制", "value": 40000, "note": "含3%损耗"},
            {"value": None},
        ]
    ) == "现货 3,000\n定制 40,000（含3%损耗）"
