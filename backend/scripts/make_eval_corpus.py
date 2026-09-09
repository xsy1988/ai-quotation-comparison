"""生成评估回归集：tests/fixtures/eval/ 下 8 份合成标注样例（xlsx + 同名 .expected.json）。

以 tests/fixtures/sample_quote.xlsx 为蓝本程序化生成（openpyxl），可重复重建：
    uv run python scripts/make_eval_corpus.py

8 份样例覆盖：基准、L1 别名命中（CNC 加工/氧化/干喷砂/阳极处理）、币种变体、
清单外工艺 L2 兜底（真空离子镀膜/激光咬花 → AT-QT-001）、勾稽有意出错（calc_check=fail）、
品类切换（塑胶 CAT-SJ）、别名+兜底混合。
"""

import json
import sys
from pathlib import Path

import openpyxl

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

EVAL_DIR = BACKEND_DIR / "tests" / "fixtures" / "eval"

# 基准条目（金额单位 元/pcs）：材料 / 加工(条目,金额) / 检验 / 包运 / 损管利税(非税)/ 税率项
BASE_MATERIAL = ("铝材", 4.50)
BASE_PROCESSING = [("CNC加工", 2.00), ("阳极氧化", 1.20), ("喷砂", 0.50)]
BASE_INSPECTION = ("全尺寸检验", 0.30)
BASE_PACKAGING = [("包装", 0.10), ("运输", 0.05)]
BASE_SGA = [("损耗", 0.15), ("管理费", 0.30), ("利润", 0.90)]
BASE_TAX = ("增值税", 0.51, "税率13%")

ATOM_CNC = "AT-QX-001"      # CNC加工
ATOM_ANODIZE = "AT-ZH-013"  # 阳极氧化
ATOM_SANDBLAST = "AT-ZP-005"  # 喷砂
ATOM_INJECT = "AT-CX-035"   # 注塑成型（含塑胶品类）
FALLBACK_ATOM = "AT-QT-001"  # 其它工艺（兜底）

# 每份样例：蓝本变体 + 标注期望
SPECS = [
    {
        "name": "eval_01_base",
        "description": "基准：不同供应商/日期，金额与蓝本一致",
        "supplier": "东莞华锐五金制品有限公司",
        "part_name": "铝合金外壳",
        "material_spec": "AL6063-T5",
        "date": "2026-08-20",
        "currency": "CNY",
        "scale": 1.0,
        "category": "CAT-WJWK",
        "expected_atoms": {"CNC加工": ATOM_CNC, "阳极氧化": ATOM_ANODIZE, "喷砂": ATOM_SANDBLAST},
        "unmatched_new_process": [],
    },
    {
        "name": "eval_02_alias_l1",
        "description": "L1 别名命中：CNC 加工 / 氧化 / 干喷砂（均应 L1 命中）",
        "supplier": "苏州精工金属科技有限公司",
        "part_name": "铝合金外壳",
        "material_spec": "AL6063-T5",
        "date": "2026-08-25",
        "currency": "CNY",
        "scale": 1.2,
        "category": "CAT-WJWK",
        "renames": {"CNC加工": "CNC 加工", "阳极氧化": "氧化", "喷砂": "干喷砂"},
        "expected_atoms": {"CNC 加工": ATOM_CNC, "氧化": ATOM_ANODIZE, "干喷砂": ATOM_SANDBLAST},
        "unmatched_new_process": [],
    },
    {
        "name": "eval_03_currency",
        "description": "币种/金额变体：USD、金额×2.0",
        "supplier": "宁波海川精密五金有限公司",
        "part_name": "铝合金外壳",
        "material_spec": "AL6063-T5",
        "date": "2026-07-15",
        "currency": "USD",
        "scale": 2.0,
        "category": "CAT-WJWK",
        "expected_atoms": {"CNC加工": ATOM_CNC, "阳极氧化": ATOM_ANODIZE, "喷砂": ATOM_SANDBLAST},
        "unmatched_new_process": [],
    },
    {
        "name": "eval_04_new_process",
        "description": "语义近义工艺：喷砂→真空离子镀膜，应 L2 语义命中 AT-ZK-012（离子镀），不判新工艺",
        "supplier": "深圳市耀达五金有限公司",
        "part_name": "铝合金外壳",
        "material_spec": "AL6063-T5",
        "date": "2026-08-28",
        "currency": "CNY",
        "scale": 1.5,
        "category": "CAT-WJWK",
        "renames": {"喷砂": "真空离子镀膜"},
        "amount_overrides": {"真空离子镀膜": 0.80},
        "expected_atoms": {"CNC加工": ATOM_CNC, "阳极氧化": ATOM_ANODIZE, "真空离子镀膜": "AT-ZK-012"},
        "unmatched_new_process": [],
    },
    {
        "name": "eval_05_calc_error",
        "description": "勾稽有意出错：含税合计/最终单价虚高 1.00，期望 calc_check=fail",
        "supplier": "佛山市顺德区铭川五金厂",
        "part_name": "铝合金外壳",
        "material_spec": "AL6063-T5",
        "date": "2026-08-30",
        "currency": "CNY",
        "scale": 1.0,
        "category": "CAT-WJWK",
        "calc_error": 1.00,
        "expected_atoms": {"CNC加工": ATOM_CNC, "阳极氧化": ATOM_ANODIZE, "喷砂": ATOM_SANDBLAST},
        "unmatched_new_process": [],
    },
    {
        "name": "eval_06_category",
        "description": "品类切换：塑胶中框（CAT-SJ），阳极氧化→注塑成型",
        "supplier": "东莞市众塑精密塑胶有限公司",
        "part_name": "ABS塑胶中框",
        "material_spec": "ABS+PC",
        "date": "2026-08-22",
        "currency": "CNY",
        "scale": 1.0,
        "category": "CAT-SJ",
        "renames": {"CNC加工": "CNC 加工", "阳极氧化": "注塑成型"},
        "expected_atoms": {"CNC 加工": ATOM_CNC, "注塑成型": ATOM_INJECT, "喷砂": ATOM_SANDBLAST},
        "unmatched_new_process": [],
    },
    {
        "name": "eval_07_mixed",
        "description": "别名+兜底混合：激光咬花应 L2 兜底，CNC 加工/干喷砂 L1 别名命中",
        "supplier": "深圳市凯盛五金制品有限公司",
        "part_name": "铝合金外壳",
        "material_spec": "AL6063-T5",
        "date": "2026-09-01",
        "currency": "CNY",
        "scale": 1.1,
        "category": "CAT-WJWK",
        "renames": {"CNC加工": "CNC 加工", "阳极氧化": "激光咬花", "喷砂": "干喷砂"},
        "expected_atoms": {"CNC 加工": ATOM_CNC, "激光咬花": FALLBACK_ATOM, "干喷砂": ATOM_SANDBLAST},
        "unmatched_new_process": ["激光咬花"],
    },
    {
        "name": "eval_08_alias_plus_new",
        "description": "别名（阳极处理/CNC 加工）+ 真空离子镀膜 L2 语义命中 AT-ZK-012",
        "supplier": "惠州志远五金加工厂",
        "part_name": "铝合金外壳",
        "material_spec": "AL6063-T5",
        "date": "2026-09-03",
        "currency": "CNY",
        "scale": 1.0,
        "category": "CAT-WJWK",
        "renames": {"CNC加工": "CNC 加工", "阳极氧化": "阳极处理", "喷砂": "真空离子镀膜"},
        "expected_atoms": {"CNC 加工": ATOM_CNC, "阳极处理": ATOM_ANODIZE, "真空离子镀膜": "AT-ZK-012"},
        "unmatched_new_process": [],
    },
]


def _amt(base: float, scale: float) -> float:
    return round(base * scale, 2)


def build_rows(spec: dict) -> tuple[list[list], float]:
    """按 spec 生成报价单行（汇总行由明细推导，保证勾稽自洽；calc_error 时故意打破）。

    返回 (行数据, 最终含税单价)。
    """
    s = spec["scale"]
    renames = spec.get("renames", {})
    overrides = spec.get("amount_overrides", {})

    def item(base_name: str, base_amount: float) -> tuple[str, float]:
        name = renames.get(base_name, base_name)
        return name, _amt(overrides.get(name, base_amount), s)

    material = item(*BASE_MATERIAL)
    processing = [item(*p) for p in BASE_PROCESSING]
    inspection = item(*BASE_INSPECTION)
    packaging = [item(*p) for p in BASE_PACKAGING]
    sga = [item(*p) for p in BASE_SGA]
    tax_name, tax_amount, tax_note = BASE_TAX
    tax_amount = _amt(tax_amount, s)

    untaxed = round(
        material[1] + sum(p[1] for p in processing) + inspection[1]
        + sum(p[1] for p in packaging) + sum(p[1] for p in sga), 2
    )
    taxed = round(untaxed + tax_amount, 2)
    calc_error = spec.get("calc_error", 0.0)
    taxed_shown = round(taxed + calc_error, 2)

    rows: list[list] = [
        ["供应商报价单", None, None, None],
        ["供应商", spec["supplier"], None, None],
        ["零件名称", spec["part_name"], "材料规格", spec["material_spec"]],
        ["报价日期", spec["date"], "币种", spec["currency"]],
        [None, None, None, None],
        ["费用项目", "明细", "金额(元/pcs)", "备注"],
        ["材料费", material[0], material[1], spec["material_spec"]],
        ["材料费合计", None, material[1], None],
    ]
    for name, amount in processing:
        rows.append(["加工费", name, amount, None])
    rows.append(["加工费合计", None, round(sum(p[1] for p in processing), 2), None])
    rows.append(["检验费", inspection[0], inspection[1], None])
    rows.append(["检验费合计", None, inspection[1], None])
    for name, amount in packaging:
        rows.append(["包装运输费", name, amount, None])
    rows.append(["包装运输费合计", None, round(sum(p[1] for p in packaging), 2), None])
    for name, amount in sga:
        rows.append(["损管利税", name, amount, None])
    rows.append(["损管利税", tax_name, tax_amount, tax_note])
    rows.append(["损管利税合计", None, round(tax_amount + sum(p[1] for p in sga), 2), None])
    rows.append(["未税合计", None, untaxed, None])
    rows.append(["税额", None, tax_amount, None])
    rows.append(["含税合计", None, taxed_shown, None])
    rows.append(["最终含税单价", None, taxed_shown, None])
    rows.append([None, None, None, None])
    rows.append(["模治具费(一次性)", "冲压成型模", _amt(25000, s), "穴数1；寿命50万模次"])
    rows.append(["模治具费(一次性)", "检具", _amt(3000, s), None])
    rows.append(["模治具费合计", None, _amt(28000, s), None])
    return rows, taxed_shown


def build_corpus(out_dir: Path = EVAL_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for spec in SPECS:
        rows, final_price = build_rows(spec)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "报价单"
        for row in rows:
            ws.append(row)

        xlsx_path = out_dir / f"{spec['name']}.xlsx"
        wb.save(xlsx_path)
        written.append(xlsx_path)

        expected = {
            "supplier_name": spec["supplier"],
            "final_unit_price_taxed": final_price,
            "calc_check": "fail" if spec.get("calc_error") else "pass",
            "category": spec["category"],
            "expected_atoms": spec["expected_atoms"],
            "unmatched_new_process": spec["unmatched_new_process"],
            "description": spec["description"],
        }
        exp_path = out_dir / f"{spec['name']}.expected.json"
        exp_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(exp_path)
    return written


def main() -> None:
    written = build_corpus()
    print(f"回归集已生成：{EVAL_DIR}（{len(SPECS)} 份样例，{len(written)} 个文件）")
    for spec in SPECS:
        print(f"  {spec['name']}: {spec['description']}")


if __name__ == "__main__":
    main()
