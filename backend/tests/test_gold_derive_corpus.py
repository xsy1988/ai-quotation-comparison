"""金标 derive 回归清单：tests/fixtures/eval/gold_derive_cases.json 登记的信封+IR 案例，
重放 derive_offer 后 summary（untaxed/tax/taxed/final）与 processing.total 必须等于金标值。

构成级断言（可选键）：
- kept_item_amounts / item_amounts: {"模块.科目名": 金额} —— 科目必须存在且 amount_per_pc
  等于期望值（kept 强调去重后保留，item 为通用逐项构成断言）；
- absent_items: ["模块.科目名", ...] —— 负断言：碎片/错挂科目（如"其它工艺"/"破氧"）不应出现。

derive/validators/prompt 的任何劣化都会在这里立刻暴露（pytest 全量跑时同步校验）；
scripts/eval_replay.py 也会重放本清单（确定性规则，无 LLM 依赖）。
"""

import json
from pathlib import Path

import pytest

from app.derive import derive_offer

BACKEND_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = BACKEND_DIR / "tests" / "fixtures" / "eval"
MANIFEST = CORPUS_DIR / "gold_derive_cases.json"


def _load_cases():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = []
    for case in manifest["cases"]:
        envelope_path = (CORPUS_DIR / case["envelope"]).resolve()
        ir_path = (CORPUS_DIR / case["ir"]).resolve()
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
        cases.append((case["name"], envelope, ir, case["expected_offers"]))
    return cases


CASES = _load_cases()


@pytest.mark.parametrize("name,envelope,ir,expected", CASES, ids=[c[0] for c in CASES])
def test_gold_derive_case(name, envelope, ir, expected):
    assert len(envelope["offers"]) == len(expected)
    for offer, exp in zip(envelope["offers"], expected):
        flags = derive_offer(offer, ir=ir)
        summary = offer["unit_price"]["summary"]
        assert summary["untaxed_total"] == pytest.approx(exp["untaxed_total"], abs=0.01)
        assert summary["tax_amount"] == pytest.approx(exp["tax_amount"], abs=0.01)
        assert summary["taxed_total"] == pytest.approx(exp["taxed_total"], abs=0.01)
        assert summary["final_unit_price_taxed"] == pytest.approx(
            exp["final_unit_price_taxed"], abs=0.01
        )
        assert offer["unit_price"]["processing"]["total"] == pytest.approx(
            exp["processing_total"], abs=0.01
        )
        # 金标 keeper 断言：如创锋案例的"全检"必须保留 0.30 不被去重置零
        for ref, expected_amount in (exp.get("kept_item_amounts") or {}).items():
            module, _, item_name = ref.partition(".")
            item = next(
                i for i in offer["unit_price"][module]["items"] if i.get("name") == item_name
            )
            assert item["amount_per_pc"] == pytest.approx(expected_amount, abs=0.01), (
                f"{name}: {ref} 期望保留 {expected_amount}，实际 {item['amount_per_pc']}"
            )
        # 构成级断言：指定模块下科目必须存在且金额相等（逐项工艺名+金额）
        for ref, expected_amount in (exp.get("item_amounts") or {}).items():
            module, _, item_name = ref.partition(".")
            item = next(
                (i for i in offer["unit_price"][module]["items"] if i.get("name") == item_name),
                None,
            )
            assert item is not None, f"{name}: {ref} 科目应存在于 {module} 模块"
            assert item["amount_per_pc"] == pytest.approx(expected_amount, abs=0.01), (
                f"{name}: {ref} 期望 {expected_amount}，实际 {item['amount_per_pc']}"
            )
        # 负断言：碎片/错挂科目不应出现（如"其它工艺"、表头碎片"破氧"/"白"）
        for ref in (exp.get("absent_items") or []):
            module, _, item_name = ref.partition(".")
            assert all(
                i.get("name") != item_name for i in offer["unit_price"][module]["items"]
            ), f"{name}: {ref} 不应出现在 {module} 模块"
        assert "shared_cell" in flags  # 创锋信封必含共享单元格去重标记
