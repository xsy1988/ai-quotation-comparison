"""评估回归脚本：对回归集逐份跑「ingest → LLM 版面理解 → 校验 → 落库 → 映射」全链
（真实 LLM 网关），与标注期望（.expected.json）对比，输出命中率报告。

用法（手动跑，不进 pytest）：
    uv run python scripts/eval_replay.py [--corpus tests/fixtures/eval] [--verbose] [--keep-workdir]

每份样例独立临时库（QUOTES_DB_PATH 指向临时文件）+ 独立归档/IR/快照目录，
主数据经 subprocess 调 scripts/import_master_data.py 灌入。失败不中止，计入报告。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_CORPUS = BACKEND_DIR / "tests" / "fixtures" / "eval"
IMPORT_SCRIPT = BACKEND_DIR / "scripts" / "import_master_data.py"

EXPECTED_KEYS = {
    "supplier_name", "final_unit_price_taxed", "calc_check",
    "category", "expected_atoms", "unmatched_new_process",
}


def _load_expected(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    missing = EXPECTED_KEYS - set(data)
    if missing:
        raise ValueError(f"{path.name} 缺少字段：{sorted(missing)}")
    return data


def _run_full_chain(xlsx: Path, workdir: Path) -> dict:
    """ingest → 版面理解 → 校验 → 落库 → 映射。返回实测结果 dict。"""
    import app.ingest as ingest
    import app.persist as persist
    from app.db import get_connection, init_db
    from app.ingest import ingest_file
    from app.ir import IR
    from app.persist import persist_quote
    from app.pipeline.layout_understand import parse_ir_with_llm_traced
    from app.pipeline.mapping_runner import run_mapping
    from app.validate.validate import validate_quote_full

    os.environ["QUOTES_DB_PATH"] = str(workdir / "quotes.db")
    ingest.ARCHIVE_DIR = workdir / "archive"
    ingest.IR_DIR = workdir / "ir"
    persist.SNAPSHOT_DIR = workdir / "snapshots"

    init_db()
    ingest_result = ingest_file(xlsx)
    ir = IR.from_dict(json.loads(Path(ingest_result["ir_path"]).read_text(encoding="utf-8")))

    conn = get_connection()
    try:
        data, attempts, cross = parse_ir_with_llm_traced(ir, conn=conn)
        calc_check, flags = validate_quote_full(conn, data)
        pres = persist_quote(data, project_name="eval", file_hash=ingest_result["sha256"])
        quote_id = pres["quote_id"]
        run_mapping(quote_id, conn)
        quote = conn.execute(
            "SELECT supplier_name, category_code FROM quote WHERE id = ?", (quote_id,)
        ).fetchone()
        lines = conn.execute(
            "SELECT item_name, atom_code, is_new_process FROM quote_line"
            " WHERE quote_id = ? AND module = 'processing' ORDER BY id",
            (quote_id,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "supplier_name": quote["supplier_name"],
        "final_unit_price_taxed": data["unit_price"]["summary"]["final_unit_price_taxed"],
        "calc_check": calc_check,
        "category": quote["category_code"],
        "atoms": {r["item_name"].strip(): r["atom_code"] for r in lines},
        "new_process": sorted(r["item_name"].strip() for r in lines if r["is_new_process"]),
        "cross_conflicts": cross["item_conflicts"] + len(cross["total_conflicts"]),
        "llm_rounds": len(attempts),
        "flags": flags,
    }


def _compare(expected: dict, actual: dict) -> dict:
    """逐维度对比，返回 {维度: (是否通过, 明细)}。"""
    dims: dict[str, dict] = {}

    price_exp = expected["final_unit_price_taxed"]
    price_act = actual["final_unit_price_taxed"]
    dims["价格"] = {
        "ok": price_act is not None and abs(float(price_act) - float(price_exp)) <= 0.011,
        "expected": price_exp, "actual": price_act,
    }
    dims["供应商"] = {
        "ok": (actual["supplier_name"] or "").strip() == expected["supplier_name"].strip(),
        "expected": expected["supplier_name"], "actual": actual["supplier_name"],
    }
    dims["勾稽"] = {
        "ok": actual["calc_check"] == expected["calc_check"],
        "expected": expected["calc_check"], "actual": actual["calc_check"],
    }
    dims["品类"] = {
        "ok": actual["category"] == expected["category"],
        "expected": expected["category"], "actual": actual["category"],
    }

    expected_atoms = expected["expected_atoms"]
    atom_hits = {
        name: actual["atoms"].get(name) == code
        for name, code in expected_atoms.items()
    }
    dims["原子命中"] = {
        "ok": all(atom_hits.values()),
        "hits": sum(atom_hits.values()), "total": len(atom_hits),
        "expected": expected_atoms, "actual": actual["atoms"],
        "detail": {k: v for k, v in atom_hits.items() if not v},
    }

    expected_new = sorted(expected["unmatched_new_process"])
    dims["L2兜底"] = {
        "ok": actual["new_process"] == expected_new,
        "expected": expected_new, "actual": actual["new_process"],
    }
    dims["交叉验证"] = {
        "ok": actual["cross_conflicts"] == 0,
        "expected": 0, "actual": actual["cross_conflicts"],
    }
    return dims


def _fmt_bool(ok: bool) -> str:
    return "✓" if ok else "✗"


def main() -> None:
    parser = argparse.ArgumentParser(description="评估回归：全链重放回归集并输出命中率报告")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="回归集目录")
    parser.add_argument("--verbose", action="store_true", help="打印每份失败维度明细")
    parser.add_argument("--keep-workdir", action="store_true", help="保留临时工作目录便于排查")
    args = parser.parse_args()

    if not args.corpus.exists():
        raise SystemExit(f"回归集目录不存在：{args.corpus}（先跑 scripts/make_eval_corpus.py）")

    xlsx_files = sorted(args.corpus.glob("*.xlsx"))
    if not xlsx_files:
        raise SystemExit(f"回归集目录无样例：{args.corpus}")

    header = f"{'样例':<24}{'价格':<6}{'供应商':<8}{'勾稽':<6}{'品类':<6}{'原子命中':<14}{'L2兜底':<8}{'交叉验证':<8}"
    print(f"回归集：{args.corpus}（{len(xlsx_files)} 份，真实网关全链重放）\n")
    print(header)
    print("-" * len(header))

    dim_totals: dict[str, list[int]] = {}
    atom_hit = atom_total = 0
    failures: list[tuple[str, dict]] = []

    for xlsx in xlsx_files:
        expected_path = xlsx.with_suffix(".expected.json")
        expected = _load_expected(expected_path)
        workdir = Path(tempfile.mkdtemp(prefix=f"eval_{xlsx.stem}_"))
        try:
            subprocess.run(
                [sys.executable, str(IMPORT_SCRIPT)],
                check=True, capture_output=True, text=True,
                env={**os.environ, "QUOTES_DB_PATH": str(workdir / "quotes.db")},
            )
            actual = _run_full_chain(xlsx, workdir)
        except Exception as e:  # 单份失败不拖垮整批
            print(f"{xlsx.stem:<24}全链失败：{type(e).__name__}: {e}")
            failures.append((xlsx.stem, {"全链": {"ok": False, "error": str(e)}}))
            for dim in ("价格", "供应商", "勾稽", "品类", "原子命中", "L2兜底", "交叉验证"):
                dim_totals.setdefault(dim, [0, 0])[1] += 1
            continue
        finally:
            if not args.keep_workdir:
                import shutil

                shutil.rmtree(workdir, ignore_errors=True)

        dims = _compare(expected, actual)
        row = (
            f"{xlsx.stem:<24}"
            f"{_fmt_bool(dims['价格']['ok']):<6}"
            f"{_fmt_bool(dims['供应商']['ok']):<8}"
            f"{_fmt_bool(dims['勾稽']['ok']):<6}"
            f"{_fmt_bool(dims['品类']['ok']):<6}"
            f"{dims['原子命中']['hits']}/{dims['原子命中']['total']:<12}"
            f"{_fmt_bool(dims['L2兜底']['ok']):<8}"
            f"{_fmt_bool(dims['交叉验证']['ok']):<8}"
        )
        print(row)
        for dim, d in dims.items():
            dim_totals.setdefault(dim, [0, 0])
            dim_totals[dim][0] += int(d["ok"])
            dim_totals[dim][1] += 1
        atom_hit += dims["原子命中"]["hits"]
        atom_total += dims["原子命中"]["total"]
        if not all(d["ok"] for d in dims.values()):
            failures.append((xlsx.stem, dims))

    print("-" * len(header))
    print("命中率：")
    for dim, (hit, total) in dim_totals.items():
        print(f"  {dim:<10}{hit}/{total}  ({hit / total * 100:.0f}%)")
    if atom_total:
        print(f"  原子命中（条目级）：{atom_hit}/{atom_total}  ({atom_hit / atom_total * 100:.0f}%)")

    if args.verbose and failures:
        print("\n== 失败明细 ==")
        for name, dims in failures:
            print(f"[{name}]")
            for dim, d in dims.items():
                if not d.get("ok"):
                    print(f"  {dim}: 期望 {d.get('expected')!r}，实际 {d.get('actual')!r}"
                          + (f"，未命中 {d['detail']}" if d.get("detail") else ""))


if __name__ == "__main__":
    main()
