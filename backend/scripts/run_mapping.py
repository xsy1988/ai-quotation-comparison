"""CLI：对一份已落库的报价单跑 L1 别名匹配 + 费用类型映射。

用法：
    uv run python scripts/run_mapping.py <quote_id>
"""

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.pipeline.mapping_runner import run_mapping  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="报价单映射：L1 别名精确匹配 + fee_classify")
    parser.add_argument("quote_id", type=int, help="quote 表主键")
    args = parser.parse_args()

    stats = run_mapping(args.quote_id)
    print("== 映射结果 ==")
    for key, value in stats.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
