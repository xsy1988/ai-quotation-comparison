"""生成 tests/fixtures/sample_quote.xlsx（样例供应商报价单，数字与 sample_quote.json 对应）。"""

from pathlib import Path

import openpyxl

FIXTURES = Path(__file__).resolve().parent


def build() -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "报价单"

    rows = [
        ["供应商报价单", None, None, None],
        ["供应商", "深圳市样例五金有限公司", None, None],
        ["零件名称", "铝合金外壳", "材料规格", "AL6063-T5"],
        ["报价日期", "2026-09-01", "币种", "CNY"],
        [None, None, None, None],
        ["费用项目", "明细", "金额(元/pcs)", "备注"],
        ["材料费", "铝材", 4.50, "AL6063-T5"],
        ["材料费合计", None, 4.50, None],
        ["加工费", "CNC加工", 2.00, None],
        ["加工费", "阳极氧化", 1.20, None],
        ["加工费", "喷砂", 0.50, None],
        ["加工费合计", None, 3.70, None],
        ["检验费", "全尺寸检验", 0.30, None],
        ["检验费合计", None, 0.30, None],
        ["包装运输费", "包装", 0.10, None],
        ["包装运输费", "运输", 0.05, None],
        ["包装运输费合计", None, 0.15, None],
        ["损管利税", "损耗", 0.15, None],
        ["损管利税", "管理费", 0.30, None],
        ["损管利税", "利润", 0.90, None],
        ["损管利税", "增值税", 0.51, "税率13%"],
        ["损管利税合计", None, 1.86, None],
        ["未税合计", None, 10.00, None],
        ["税额", None, 0.51, None],
        ["含税合计", None, 10.51, None],
        ["最终含税单价", None, 10.51, None],
        [None, None, None, None],
        ["模治具费(一次性)", "冲压成型模", 25000, "穴数1；寿命50万模次"],
        ["模治具费(一次性)", "检具", 3000, None],
        ["模治具费合计", None, 28000, None],
    ]
    for row in rows:
        ws.append(row)
    ws.merge_cells("A1:D1")

    out = FIXTURES / "sample_quote.xlsx"
    wb.save(out)
    return out


if __name__ == "__main__":
    print(f"written: {build()}")
