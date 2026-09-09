"""回归集 smoke 测试：make_eval_corpus 可重建 8 份样例 + 标注文件齐全且自洽（非 LLM 部分）。"""

import importlib.util
import json
import sys
from pathlib import Path

import openpyxl
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = BACKEND_DIR / "tests" / "fixtures" / "eval"


def _load_corpus_module():
    spec = importlib.util.spec_from_file_location(
        "make_eval_corpus", BACKEND_DIR / "scripts" / "make_eval_corpus.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_corpus_dir_has_8_pairs():
    xlsx_files = sorted(CORPUS_DIR.glob("*.xlsx"))
    expected_files = sorted(CORPUS_DIR.glob("*.expected.json"))
    assert len(xlsx_files) == 8, f"回归集应有 8 份样例，实际 {len(xlsx_files)}"
    assert len(expected_files) == 8
    assert {p.with_suffix("").name for p in xlsx_files} == {
        p.name[: -len(".expected.json")] for p in expected_files
    }


@pytest.mark.parametrize("expected_path", sorted(CORPUS_DIR.glob("*.expected.json")), ids=lambda p: p.name)
def test_expected_file_wellformed(expected_path):
    data = json.loads(expected_path.read_text(encoding="utf-8"))
    assert set(data) >= {
        "supplier_name", "final_unit_price_taxed", "calc_check",
        "category", "expected_atoms", "unmatched_new_process",
    }
    assert data["calc_check"] in ("pass", "fail")
    assert data["category"].startswith("CAT-")
    assert all(code.startswith("AT-") for code in data["expected_atoms"].values())
    assert set(data["unmatched_new_process"]) <= set(data["expected_atoms"])


@pytest.mark.parametrize("xlsx_path", sorted(CORPUS_DIR.glob("*.xlsx")), ids=lambda p: p.name)
def test_corpus_xlsx_matches_annotation(xlsx_path):
    """样例 xlsx 与标注一致：供应商名出现在文件中、最终含税单价与标注相等。"""
    data = json.loads(
        (CORPUS_DIR / f"{xlsx_path.stem}.expected.json").read_text(encoding="utf-8")
    )
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["报价单"]
    flat = {str(cell.value) for row in ws.iter_rows() for cell in row if cell.value is not None}
    assert data["supplier_name"] in flat

    summary_labels = {"未税合计": None, "税额": None, "含税合计": None, "最终含税单价": None}
    for row in ws.iter_rows(values_only=True):
        if row[0] in summary_labels:
            summary_labels[row[0]] = row[2]
    assert summary_labels["最终含税单价"] is not None
    assert abs(float(summary_labels["最终含税单价"]) - float(data["final_unit_price_taxed"])) < 1e-6

    # 勾稽自洽性：pass 样例必须满足 含税 = 未税 + 税额
    if data["calc_check"] == "pass":
        untaxed = float(summary_labels["未税合计"])
        tax = float(summary_labels["税额"])
        taxed = float(summary_labels["含税合计"])
        assert abs(untaxed + tax - taxed) < 0.011, f"{xlsx_path.name} 勾稽不自洽"
    wb.close()


def test_rebuild_corpus_into_tmpdir(tmp_path):
    """make_eval_corpus.build_corpus 可重建完整回归集（程序生成、不依赖手工文件）。"""
    module = _load_corpus_module()
    written = module.build_corpus(tmp_path)
    assert len([p for p in written if p.suffix == ".xlsx"]) == 8
    assert len([p for p in written if p.suffix == ".json"]) == 8


def test_eval_replay_loads_expected():
    """eval_replay 的期望文件加载逻辑对 8 份标注全部可用。"""
    sys.path.insert(0, str(BACKEND_DIR / "scripts"))
    try:
        import eval_replay
    finally:
        sys.path.remove(str(BACKEND_DIR / "scripts"))
    for path in sorted(CORPUS_DIR.glob("*.expected.json")):
        expected = eval_replay._load_expected(path)
        assert expected["supplier_name"]
