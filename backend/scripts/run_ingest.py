"""CLI：文件接入 + （可选）persist 全链路演示。

用法：
    uv run python scripts/run_ingest.py <excel文件> [--force] [--demo-persist <quote.json>]
"""

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.ingest import ingest_file  # noqa: E402
from app.persist import persist_quote  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="报价文件接入（查重 → Excel→IR → 归档）")
    parser.add_argument("excel", type=Path, help="报价单 Excel 文件")
    parser.add_argument("--force", action="store_true", help="强制重新接入（忽略查重门禁）")
    parser.add_argument("--demo-persist", type=Path, help="继续跑 persist：quote_schema JSON 落库")
    args = parser.parse_args()

    result = ingest_file(args.excel, force=args.force)
    print("== 接入结果 ==")
    for key, value in result.items():
        print(f"  {key}: {value}")

    if result["status"] == "reused":
        print("查重命中：已解析过的文件直接复用历史 IR，不重复接入。")
        if not args.force:
            print("（如需重跑接入请加 --force）")

    if args.demo_persist:
        data = json.loads(args.demo_persist.read_text(encoding="utf-8"))
        print(f"\n== persist：{args.demo_persist.name} ==")
        summary = persist_quote(data)
        for key, value in summary.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
